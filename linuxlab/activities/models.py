from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q

from linuxlab.core.models import StableIdentityModel


class ActivityDefinition(StableIdentityModel):
    """A learning extension identity (ADR-004). Provider-specific configuration
    belongs to ActivityVersion, not here. type_key is intentionally not unique:
    many Activities may use the same provider.
    """

    name = models.CharField(max_length=255)
    type_key = models.CharField(max_length=100)


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
        if self.pk is None:
            super().save(*args, **kwargs)
            return

        with transaction.atomic():
            persisted_published_at = (
                ActivityVersion.objects.select_for_update()
                .filter(pk=self.pk)
                .values_list("published_at", flat=True)
                .first()
            )
            if persisted_published_at is not None:
                raise ValueError("published ActivityVersion is immutable")
            super().save(*args, **kwargs)
