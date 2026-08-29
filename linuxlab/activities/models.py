from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q

from linuxlab.core.models import StableIdentityModel


class ActivityDefinition(StableIdentityModel):
    """A learning extension identity (ADR-004). Provider-specific configuration
    belongs to ActivityVersion, not here. type_key is intentionally not unique:
    many Activities may use the same provider.

    type_key is immutable once the definition has been created: every
    ActivityVersion under one ActivityDefinition belongs to one provider
    family, so reinterpreting that family after published versions exist
    would silently change how they're graded/evaluated. Changing provider
    means creating a different ActivityDefinition (Task 06 correction).
    """

    name = models.CharField(max_length=255)
    type_key = models.CharField(max_length=100)

    def save(self, *args, **kwargs):
        if self.pk is not None:
            persisted_type_key = (
                ActivityDefinition.objects.filter(pk=self.pk)
                .values_list("type_key", flat=True)
                .first()
            )
            if persisted_type_key is not None and persisted_type_key != self.type_key:
                raise ValueError("ActivityDefinition.type_key is immutable after creation")
        super().save(*args, **kwargs)


class ActivityVersion(models.Model):
    """An exact, eventually-immutable version of one ActivityDefinition.

    Distributed identity is ActivityDefinition.public_id + version; this model
    intentionally has no public_id of its own (ADR-011, ADR-014).
    """

    definition = models.ForeignKey(
        ActivityDefinition,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    config = models.JSONField(default=dict, blank=True)
    required_capabilities = models.JSONField(default=list, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["definition", "version"],
                name="unique_activity_definition_version",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="activity_version_number_gte_1",
            ),
        ]

    @property
    def is_published(self) -> bool:
        return self.published_at is not None

    def save(self, *args, **kwargs):
        # Every field on this model is part of the published snapshot, so once
        # the persisted row is published, any further save is rejected outright
        # rather than diffed field-by-field (ADR-014). The transition that sets
        # published_at itself is allowed, since it checks the row's prior state.
        #
        # The published-state check and the write must happen under the same
        # row lock: a plain (unlocked) SELECT-then-UPDATE would let a stale
        # in-memory instance overwrite a row published concurrently by another
        # transaction between the check and the write.
        #
        # self.pk is None is NOT equivalent to Django's insertion state
        # (self._state.adding): an unsaved instance can carry an explicit pk
        # (unused, or colliding with an existing row), and Django's save()
        # decides UPDATE-vs-INSERT purely from whether pk is set, not from
        # _state.adding. Using pk-is-None here would let such an object fall
        # into the existing-row branch below and either insert with a
        # caller-chosen local PK or silently overwrite an existing
        # unpublished row it never actually loaded (cross-task Task 07
        # lesson). Local PK is a private implementation detail (ADR-007,
        # ADR-011); no accepted caller ever supplies it explicitly.
        if self._state.adding:
            if self.pk is not None:
                raise ValueError(
                    "ActivityVersion's local primary key is assigned by the "
                    "database and cannot be supplied by ordinary application code"
                )
            super().save(*args, **kwargs)
            return

        with transaction.atomic():
            persisted_row = (
                ActivityVersion.objects.select_for_update()
                .filter(pk=self.pk)
                .values("published_at")
                .first()
            )
            if persisted_row is None:
                # A persisted-state object whose row no longer exists must
                # not be silently resurrected via INSERT fallback.
                raise ValueError("ActivityVersion no longer exists")
            if persisted_row["published_at"] is not None:
                raise ValueError("published ActivityVersion is immutable")
            super().save(*args, **kwargs)
