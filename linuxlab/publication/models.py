from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class PublicationSnapshotQuerySet(models.QuerySet):
    """Blocks the two bulk write paths that never call Model.save() and so
    would otherwise bypass PublicationSnapshot.save()'s unconditional
    rejection below (Task 09 corrective, Critical 1): QuerySet.bulk_create()
    builds rows with a raw multi-row INSERT, and QuerySet.update() issues a
    raw UPDATE against already-matched rows -- neither ever routes through
    the model's overridden save().
    """

    def bulk_create(self, objs, *args, **kwargs):
        raise ValueError(
            "PublicationSnapshot cannot be created through QuerySet.bulk_create(); "
            "publication is the only writer"
        )

    def update(self, **kwargs):
        raise ValueError(
            "PublicationSnapshot cannot be mutated through QuerySet.update(); "
            "publication snapshots are immutable once created"
        )


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

    Trustworthy-by-construction (Task 09 corrective, Critical 1): ordinary
    application code cannot create a row here AT ALL, not even a first,
    otherwise-valid one -- save() unconditionally rejects (which also blocks
    Manager.create()/get_or_create()/update_or_create(), since those all
    call save() internally), and PublicationSnapshotQuerySet blocks the two
    write paths that don't. The publication service is the sole legitimate
    writer; it inserts through
    linuxlab.publication.snapshots._insert_published_snapshot(), which
    deliberately bypasses save() via Model.save_base() -- the same bypass
    spirit as Task 07's controlled QuerySet.update() for Attempt -- and only
    after every publication invariant (dependency validation, coherent
    capture, deterministic hashing, version allocation) has already passed.
    """

    kind = models.CharField(max_length=100)
    source_public_id = models.UUIDField()
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    payload = models.JSONField()
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = models.Manager.from_queryset(PublicationSnapshotQuerySet)()

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
        raise ValueError(
            "PublicationSnapshot cannot be created or modified through ordinary "
            "application code; publication is the only writer"
        )
