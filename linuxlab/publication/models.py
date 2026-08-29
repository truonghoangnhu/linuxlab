from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class PublicationSnapshotQuerySet(models.QuerySet):
    """Blocks the bulk write paths that never call Model.save() and so would
    otherwise bypass PublicationSnapshot.save()'s unconditional rejection
    below (Task 09 corrective, Critical 1): QuerySet.bulk_create() builds
    rows with a raw multi-row INSERT, and QuerySet.update()/bulk_update()
    issue raw UPDATEs against already-matched rows -- none of these ever
    route through the model's overridden save(). This queryset only guards
    writes; every read method (all(), filter(), get(), ...) is untouched.
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

    def bulk_update(self, objs, fields, *args, **kwargs):
        raise ValueError(
            "PublicationSnapshot cannot be mutated through QuerySet.bulk_update(); "
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
    call save() internally), and PublicationSnapshotQuerySet blocks the
    write paths that don't. That guard is bound to EVERY manager Django
    exposes, not just `objects`: base_manager_name (Meta, below) makes
    `_base_manager` -- otherwise a bare, unguarded models.Manager() by
    Django's default, regardless of what `objects` is -- resolve to this
    same guarded queryset too, closing a live bulk_create()/update()/
    bulk_update() bypass a prior corrective missed (`_default_manager` was
    always guarded; `_base_manager` was not). The publication service is
    the sole legitimate writer; it inserts through
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
        # Without this, Model._base_manager defaults to a bare, unguarded
        # models.Manager() regardless of what `objects` is -- Django only
        # uses the first declared manager for _default_manager, not
        # _base_manager (Task 09 corrective, Critical: _base_manager was a
        # live bulk_create/update/bulk_update bypass). Pointing
        # base_manager_name at the SAME guarded manager closes it without
        # a second manager class or a filtering base manager -- reads via
        # _base_manager (used internally by e.g. delete() collectors and
        # relation traversal) are completely unaffected, since
        # PublicationSnapshotQuerySet only overrides write paths.
        base_manager_name = "objects"
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
