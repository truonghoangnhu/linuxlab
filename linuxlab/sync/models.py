"""Node package lifecycle inventory (ADR-008, ADR-010, ADR-014, Task 12).

Task 11's `.llpkg` format (linuxlab/publication/package.py) proves a package
is authentic and self-contained; this module stores what a Node has locally
staged/activated from that trust boundary. It never re-derives trust: every
row here exists only because linuxlab.sync.services already verified the
package bytes it was built from.
"""

from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

STAGED = "staged"
ACTIVE = "active"
ARCHIVED = "archived"

PACKAGE_STATE_CHOICES = [
    (STAGED, "Staged"),
    (ACTIVE, "Active"),
    (ARCHIVED, "Archived"),
]


class InstalledPackage(models.Model):
    """One Node-local package lifecycle row.

    Distributed identity is (package_type, source_public_id,
    package_version), mirroring Task 11's PackageManifest
    (package_type/package_uuid/package_version) exactly; this model
    intentionally has no public_id of its own, matching the
    PublicationSnapshot/ActivityVersion pattern (ADR-011) -- local primary
    keys never leave this Node.

    state/activated_at/previous_package are the only fields that change
    after creation, and only through linuxlab.sync.services -- every
    semantic artifact this package owns (InstalledPublication,
    InstalledActivityVersion, InstalledQuestionVersion,
    InstalledQuestionGrading) is immutable once staged (see
    ImmutableArtifact below), so activation/rollback are pure lifecycle
    metadata transitions that never touch content.
    """

    package_type = models.CharField(max_length=100)
    source_public_id = models.UUIDField()
    package_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    payload_sha256 = models.CharField(max_length=64)

    schema_version = models.PositiveIntegerField()
    minimum_linuxlab_version = models.CharField(max_length=100)
    signature_algorithm = models.CharField(max_length=50)
    manifest_created_at = models.DateTimeField()

    state = models.CharField(max_length=20, choices=PACKAGE_STATE_CHOICES, default=STAGED)
    imported_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    previous_package = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["package_type", "source_public_id", "package_version"],
                name="unique_installed_package_identity",
            ),
            models.UniqueConstraint(
                fields=["package_type", "source_public_id"],
                condition=Q(state=ACTIVE),
                name="unique_active_installed_package_per_source",
            ),
            models.CheckConstraint(
                condition=Q(package_version__gte=1),
                name="installed_package_version_gte_1",
            ),
        ]


class ImmutableArtifactQuerySet(models.QuerySet):
    """Blocks the bulk write paths that never call Model.save() (Task 09's
    PublicationSnapshotQuerySet pattern, linuxlab/publication/models.py):
    QuerySet.update()/bulk_update() issue raw UPDATEs that would otherwise
    bypass save()'s rejection below. Read methods are untouched."""

    def update(self, **kwargs):
        raise ValueError(f"{self.model.__name__} rows are immutable and cannot be updated")

    def bulk_update(self, objs, fields, *args, **kwargs):
        raise ValueError(f"{self.model.__name__} rows are immutable and cannot be updated")


class ImmutableArtifact(models.Model):
    """Shared immutable-after-creation guard for staged package artifacts
    (ActivityVersion's pattern, linuxlab/activities/models.py): allows the
    one genuine INSERT that stages a verified row, rejects a caller-
    supplied local primary key on that insert, and rejects every
    subsequent save() outright. These rows are derived from authenticated
    package bytes (Task 11) and must never become ordinary mutable content
    after import.
    """

    objects = models.Manager.from_queryset(ImmutableArtifactQuerySet)()

    class Meta:
        abstract = True
        base_manager_name = "objects"

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.pk is not None:
                raise ValueError(
                    f"{type(self).__name__}'s local primary key is assigned by "
                    "the database and cannot be supplied by ordinary application code"
                )
            super().save(*args, **kwargs)
            return
        raise ValueError(f"{type(self).__name__} is immutable after creation")


class InstalledPublication(ImmutableArtifact):
    """The Task 11 payload.publication section, staged verbatim for exactly
    one InstalledPackage."""

    package = models.OneToOneField(
        InstalledPackage, on_delete=models.CASCADE, related_name="publication"
    )
    kind = models.CharField(max_length=100)
    source_public_id = models.UUIDField()
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    content_hash = models.CharField(max_length=64)
    payload = models.JSONField()

    class Meta(ImmutableArtifact.Meta):
        pass


class InstalledActivityVersion(ImmutableArtifact):
    """One payload.activities[*] entry, staged verbatim (learner-safe:
    provider config only, never grading data)."""

    package = models.ForeignKey(
        InstalledPackage, on_delete=models.CASCADE, related_name="activities"
    )
    activity_public_id = models.UUIDField()
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    type_key = models.CharField(max_length=100)
    config = models.JSONField()

    class Meta(ImmutableArtifact.Meta):
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["package", "activity_public_id", "version"],
                name="unique_installed_activity_version_per_package",
            ),
        ]


class InstalledQuestionVersion(ImmutableArtifact):
    """One payload.questions[*] entry -- learner-safe question content
    only, structurally separate from InstalledQuestionGrading below."""

    package = models.ForeignKey(
        InstalledPackage, on_delete=models.CASCADE, related_name="questions"
    )
    question_public_id = models.UUIDField()
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    type_key = models.CharField(max_length=100)
    content = models.JSONField()

    class Meta(ImmutableArtifact.Meta):
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["package", "question_public_id", "version"],
                name="unique_installed_question_version_per_package",
            ),
        ]


class InstalledQuestionGrading(ImmutableArtifact):
    """One payload.server.questions[*] entry -- server-only grading
    secrets (answer_key/scoring), kept in a structurally separate
    model/table from InstalledQuestionVersion so no default query path
    returns learner content and grading secrets together (ADR-013 answer
    secrecy)."""

    question = models.OneToOneField(
        InstalledQuestionVersion, on_delete=models.CASCADE, related_name="grading"
    )
    answer_key = models.JSONField()
    scoring = models.JSONField()

    class Meta(ImmutableArtifact.Meta):
        pass
