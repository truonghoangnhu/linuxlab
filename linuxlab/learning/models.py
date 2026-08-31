from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q

from linuxlab.activities.models import ActivityDefinition, ActivityVersion
from linuxlab.core.models import StableIdentityModel
from linuxlab.identity.models import Learner
from linuxlab.sync.models import InstalledActivityVersion

_ATTEMPT_IMMUTABLE_AFTER_CREATION_FIELDS = (
    "learner_id",
    "activity_version_id",
    "installed_activity_version_id",
    "started_at",
    "submission",
    "submitted_at",
)


class Attempt(StableIdentityModel):
    """One learner interaction with ONE exact immutable execution source
    (ADR-004, ADR-009, T15-D27): either a Central-authored ActivityVersion
    or a Node InstalledActivityVersion, never both and never neither. The
    FK is never re-resolved through "latest" lookup: an Attempt is
    permanently bound to the exact row it was started against, even after
    a later version publishes or a newer package activates.

    Lifecycle is expressed through timestamps, not a status field:
    submitted_at is NULL until submission completes exactly once.
    """

    learner = models.ForeignKey(
        Learner,
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    activity_version = models.ForeignKey(
        ActivityVersion,
        on_delete=models.PROTECT,
        related_name="attempts",
        null=True,
        blank=True,
    )
    installed_activity_version = models.ForeignKey(
        InstalledActivityVersion,
        on_delete=models.PROTECT,
        related_name="attempts",
        null=True,
        blank=True,
    )
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submission = models.JSONField(null=True, blank=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=(
                    Q(activity_version__isnull=False, installed_activity_version__isnull=True)
                    | Q(activity_version__isnull=True, installed_activity_version__isnull=False)
                ),
                name="attempt_exactly_one_execution_source",
            ),
        ]

    def save(self, *args, **kwargs):
        # Attempt is historical evidence (ADR-003), and finalization is
        # SERVICE-ONLY: submission/submitted_at are owned entirely by
        # submit_attempt(), never by ordinary save(). A new Attempt must
        # always start unsubmitted; an existing Attempt's learner/
        # activity_version/started_at/submission/submitted_at can never
        # change through this method at all, submitted or not. The check
        # reads the LOCKED persisted row, not this possibly-stale in-memory
        # instance, so a stale pre-submit copy can never overwrite state a
        # concurrent submit_attempt() already committed (Task 05/06 lesson:
        # ActivityVersion.save()/QuestionVersion.save() use the same pattern).
        # submit_attempt() bypasses this guard deliberately via a controlled
        # QuerySet.update() rather than calling save() for the transition.
        #
        # self.pk is None is NOT the same as "this is an insert": Django's
        # actual insertion-state flag is self._state.adding. An unsaved
        # instance can have an explicit pk (unused, or colliding with an
        # existing row) while _state.adding is still True -- that must not
        # be allowed to fall through to an existing-row UPDATE/resurrecting
        # INSERT, since it would bypass every check below entirely.
        if self._state.adding:
            if self.pk is not None:
                # Local PK is a private implementation detail (ADR-007,
                # ADR-011): ordinary application code never assigns it.
                # public_id is the stable identity callers may reference.
                raise ValueError(
                    "Attempt's local primary key is assigned by the database "
                    "and cannot be supplied by ordinary application code"
                )
            if self.submission is not None or self.submitted_at is not None:
                raise ValueError(
                    "a new Attempt must be created unsubmitted "
                    "(submission/submitted_at must be None; use submit_attempt())"
                )
            super().save(*args, **kwargs)
            return

        with transaction.atomic():
            locked = (
                Attempt.objects.select_for_update()
                .filter(pk=self.pk)
                .values(*_ATTEMPT_IMMUTABLE_AFTER_CREATION_FIELDS)
                .first()
            )
            if locked is None:
                # A persisted-state object whose row no longer exists must
                # not be silently resurrected via INSERT fallback.
                raise ValueError("Attempt no longer exists")

            for field in _ATTEMPT_IMMUTABLE_AFTER_CREATION_FIELDS:
                if getattr(self, field) != locked[field]:
                    raise ValueError(f"Attempt.{field} is immutable via normal save")

            super().save(*args, **kwargs)


class Result(models.Model):
    """The immutable, authoritative outcome of one Attempt (ADR-003).

    Exactly one Result per Attempt (OneToOneField + its implicit unique
    constraint). Once created, normal save paths cannot rewrite it: this is
    historical learning evidence, not a mutable field bag (Task 05/06
    published-immutability lesson applied to a create-only record).
    """

    attempt = models.OneToOneField(
        Attempt,
        on_delete=models.CASCADE,
        related_name="result",
    )
    score = models.IntegerField(validators=[MinValueValidator(0)])
    max_score = models.IntegerField(validators=[MinValueValidator(0)])
    details = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(score__gte=0) & Q(max_score__gte=0) & Q(score__lte=models.F("max_score")),
                name="result_score_within_max_score",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("Result is immutable once created")
        super().save(*args, **kwargs)


class Progress(models.Model):
    """Derived projection of a learner's status for one stable Activity
    definition identity (T15-D27: activity_definition_public_id), across
    both Central-authored and Node-installed execution sources.

    NOT the source of truth (ADR-003): every field here must be fully
    reconstructable from Attempt/Result history alone. Rows only exist for
    (learner, activity_definition_public_id) pairs that have at least one
    Result; rebuild_progress()/the post-submit refresh are the only
    writers.

    activity_definition is populated only when a matching local
    ActivityDefinition row exists (always true on Central for content it
    authored; never true on Node for package-only content, T15-D27 S4.3).
    activity_definition_public_id is always populated -- it is the stable
    cross-deployment identity Progress is actually keyed by.
    """

    learner = models.ForeignKey(
        Learner,
        on_delete=models.CASCADE,
        related_name="progress_records",
    )
    activity_definition = models.ForeignKey(
        ActivityDefinition,
        on_delete=models.CASCADE,
        related_name="progress_records",
        null=True,
        blank=True,
    )
    activity_definition_public_id = models.UUIDField(null=True, blank=True)
    completed = models.BooleanField()
    best_score = models.IntegerField(validators=[MinValueValidator(0)])

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["learner", "activity_definition_public_id"],
                name="unique_progress_per_learner_activity_public_id",
            ),
        ]

    def save(self, *args, **kwargs):
        # T15-D27 S4.3: activity_definition_public_id is the stable identity
        # Progress is actually keyed by; it must always be present. Callers
        # that already resolved the public_id (the projection service) pass
        # it explicitly; older/simpler callers (tests, Central-only code)
        # may pass only activity_definition, and it is derived here so
        # existing call sites remain backward-compatible without an extra
        # explicit field on every call.
        if self.activity_definition_id is None and self.activity_definition_public_id is None:
            raise ValueError(
                "Progress requires activity_definition or activity_definition_public_id"
            )
        if self.activity_definition_id is not None and self.activity_definition_public_id is None:
            self.activity_definition_public_id = self.activity_definition.public_id
        super().save(*args, **kwargs)
