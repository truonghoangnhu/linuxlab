from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class PublicationSnapshot(models.Model):
    """An exact, immutable LinuxLab publication of one authoring source
    (ADR-014). Distributed identity is (kind, source_public_id, version);
    this model intentionally has no public_id of its own, matching the
    ActivityVersion/QuestionVersion pattern (ADR-011).

    Wagtail revisions are editorial history, NOT this artifact: a
    PublicationSnapshot never references a Wagtail Revision as its identity,
    and once created it is fully immutable -- there is no draft/publish
    transition to protect, unlike ActivityVersion/QuestionVersion. A
    correction always means a new version, never a rewrite.
    """

    kind = models.CharField(max_length=100)
    source_public_id = models.UUIDField()
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    payload = models.JSONField()
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["kind", "source_public_id", "version"],
                name="unique_publication_snapshot_version",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="publication_snapshot_version_gte_1",
            ),
        ]

    def save(self, *args, **kwargs):
        # self.pk is None is NOT Django's insertion state (self._state.adding):
        # an unsaved instance can carry an explicit pk, and Django's save()
        # decides UPDATE-vs-INSERT purely from whether pk is set (Task 05-07
        # lesson). Local PK is a private implementation detail (ADR-007,
        # ADR-011); no caller may supply it. Unlike ActivityVersion/
        # QuestionVersion, there is no "unpublished but still mutable" state
        # to preserve here, so any non-adding save is simply always rejected
        # -- this also means a stale instance whose row was deleted can never
        # be resurrected, since super().save() is never reached in that branch.
        if self._state.adding:
            if self.pk is not None:
                raise ValueError(
                    "PublicationSnapshot's local primary key is assigned by "
                    "the database and cannot be supplied by ordinary "
                    "application code"
                )
            super().save(*args, **kwargs)
            return

        raise ValueError("PublicationSnapshot is immutable once created")
