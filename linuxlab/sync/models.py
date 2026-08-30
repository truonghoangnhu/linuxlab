"""Node package lifecycle inventory (ADR-008, ADR-010, ADR-014, Task 12).

Task 11's `.llpkg` format (linuxlab/publication/package.py) proves a package
is authentic and self-contained; this module stores what a Node has locally
staged/activated from that trust boundary. It never re-derives trust: every
row here exists only because linuxlab.sync.services already verified the
package bytes it was built from.

Task 12 corrective (security/architecture review): ordinary supported ORM
APIs (Model(...).save(), Manager.create(), QuerySet.bulk_create()/update()/
bulk_update()/delete(), instance.delete()) are NOT an alternative package-
import or package-mutation mechanism. Every model below rejects all of
these outright; the only legitimate writers are the private trusted
insertion/lifecycle helpers in linuxlab.sync.services, which bypass these
guards deliberately via Django's own underlying primitives (the same
bypass spirit as Task 09's PublicationSnapshot _insert_published_snapshot()
calling Model.save_base() directly).
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


class InstalledPackageQuerySet(models.QuerySet):
    """Blocks every bulk write path for InstalledPackage (Task 09's
    PublicationSnapshotQuerySet pattern): ordinary application code must
    never create, mutate, or delete package inventory rows. The private
    trusted insertion/lifecycle helpers in linuxlab.sync.services bypass
    this guard deliberately by calling Django's underlying QuerySet
    methods directly, never by calling through this subclass. Read
    methods (all(), filter(), get(), select_for_update(), ...) are
    untouched.
    """

    def create(self, **kwargs):
        raise ValueError(
            "InstalledPackage cannot be created through ordinary application "
            "code; import_package() is the only writer"
        )

    def bulk_create(self, objs, *args, **kwargs):
        raise ValueError(
            "InstalledPackage cannot be created through QuerySet.bulk_create(); "
            "import_package() is the only writer"
        )

    def update(self, **kwargs):
        raise ValueError(
            "InstalledPackage cannot be mutated through QuerySet.update(); "
            "use the lifecycle services in linuxlab.sync.services"
        )

    def bulk_update(self, objs, fields, *args, **kwargs):
        raise ValueError(
            "InstalledPackage cannot be mutated through QuerySet.bulk_update()"
        )

    def delete(self):
        raise ValueError(
            "InstalledPackage cannot be deleted through ordinary application "
            "code; package deletion/GC is out of Task 12 scope"
        )


class InstalledPackage(models.Model):
    """One Node-local package lifecycle row.

    Distributed identity is (package_type, source_public_id,
    package_version), mirroring Task 11's PackageManifest
    (package_type/package_uuid/package_version) exactly; this model
    intentionally has no public_id of its own, matching the
    PublicationSnapshot/ActivityVersion pattern (ADR-011) -- local primary
    keys never leave this Node.

    Every field here is either PROVENANCE (authenticated at import time,
    permanently immutable) or LIFECYCLE (state/activated_at/
    previous_package, mutable only through linuxlab.sync.services'
    private lifecycle helper). Ordinary save()/delete() are rejected
    unconditionally, exactly like PublicationSnapshot -- there is no
    supported ORM path that creates, mutates, or deletes a row here at
    all; the trusted importer and lifecycle services bypass this guard
    deliberately through private helpers, never through this class's own
    public surface.
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

    objects = models.Manager.from_queryset(InstalledPackageQuerySet)()

    class Meta:
        # Same reasoning as PublicationSnapshot (linuxlab/publication/models.py):
        # without this, Model._base_manager defaults to a bare, unguarded
        # models.Manager() regardless of what `objects` is, which would
        # leave a live bulk_create()/update()/bulk_update()/delete()
        # bypass through internal Django machinery that resolves via
        # _base_manager (e.g. delete() collectors, relation traversal).
        base_manager_name = "objects"
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
            models.CheckConstraint(
                condition=Q(state__in=[STAGED, ACTIVE, ARCHIVED]),
                name="installed_package_state_allowed_values",
            ),
        ]

    def save(self, *args, **kwargs):
        raise ValueError(
            "InstalledPackage cannot be created or modified through ordinary "
            "application code; import_package() and the lifecycle services in "
            "linuxlab.sync.services are the only writers"
        )

    def delete(self, *args, **kwargs):
        raise ValueError(
            "InstalledPackage cannot be deleted; package deletion/GC is out of "
            "Task 12 scope"
        )


class ImmutableArtifactQuerySet(models.QuerySet):
    """Blocks every bulk write path for staged package artifacts (Task 09's
    PublicationSnapshotQuerySet pattern): create()/bulk_create() would
    fabricate rows with no verified provenance, and update()/bulk_update()/
    delete() would mutate or destroy authenticated content after import.
    The private trusted insertion helper in linuxlab.sync.services bypasses
    this guard deliberately by calling Django's underlying Model.save_base()
    directly, never through this subclass. Read methods are untouched.
    """

    def create(self, **kwargs):
        raise ValueError(
            f"{self.model.__name__} cannot be created through ordinary "
            "application code; only a trusted package import may stage one"
        )

    def bulk_create(self, objs, *args, **kwargs):
        raise ValueError(
            f"{self.model.__name__} cannot be created through "
            "QuerySet.bulk_create(); only a trusted package import may stage one"
        )

    def update(self, **kwargs):
        raise ValueError(f"{self.model.__name__} rows are immutable and cannot be updated")

    def bulk_update(self, objs, fields, *args, **kwargs):
        raise ValueError(f"{self.model.__name__} rows are immutable and cannot be updated")

    def delete(self):
        raise ValueError(f"{self.model.__name__} rows are immutable and cannot be deleted")


class ImmutableArtifact(models.Model):
    """Shared trust guard for staged package artifacts: ordinary save()/
    delete() are rejected unconditionally (no supported ORM path creates,
    modifies, or deletes a row here), and the guarded QuerySet above closes
    every bulk write path too. These rows are derived from authenticated
    package bytes (Task 11) and must never become ordinary mutable content,
    fabricated content, or disappear as a side effect of ordinary
    application code -- an ACTIVE or ARCHIVED package must keep every
    artifact it needs to serve/roll back to.
    """

    objects = models.Manager.from_queryset(ImmutableArtifactQuerySet)()

    class Meta:
        abstract = True
        base_manager_name = "objects"

    def save(self, *args, **kwargs):
        raise ValueError(
            f"{type(self).__name__} cannot be created or modified through "
            "ordinary application code; only a trusted package import may stage one"
        )

    def delete(self, *args, **kwargs):
        raise ValueError(f"{type(self).__name__} rows are immutable and cannot be deleted")


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
