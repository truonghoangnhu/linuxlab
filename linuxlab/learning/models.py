from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from linuxlab.activities.models import ActivityDefinition, ActivityVersion
from linuxlab.core.models import StableIdentityModel
from linuxlab.identity.models import Learner


class Attempt(StableIdentityModel):
    """One learner interaction with ONE exact immutable ActivityVersion (ADR-004,
    ADR-009). The FK below is never re-resolved through "latest" lookup: an
    Attempt is permanently bound to the ActivityVersion row it was started
    against, even after later versions of the same ActivityDefinition publish.

    Lifecycle is expressed through timestamps, not a status field:
    submitted_at is NULL until submit_attempt() completes exactly once.
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
    )
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submission = models.JSONField(null=True, blank=True)


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
    """Derived projection of a learner's status for one ActivityDefinition.

    NOT the source of truth (ADR-003): every field here must be fully
    reconstructable from Attempt/Result history alone. Rows only exist for
    (learner, activity_definition) pairs that have at least one Result;
    rebuild_progress()/the post-submit refresh are the only writers.
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
    )
    completed = models.BooleanField()
    best_score = models.IntegerField(validators=[MinValueValidator(0)])

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["learner", "activity_definition"],
                name="unique_progress_per_learner_activity",
            ),
        ]
