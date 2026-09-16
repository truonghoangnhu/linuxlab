"""Node package lifecycle inventory (ADR-008, ADR-010, ADR-014, Task 12) and
Node <-> Central event sync persistence (ADR-010, Task 14).

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

Task 14 (T14-D10, T14-D11, T14-D26, T14-D31): NodeRegistration,
OutboxMessage, and InboxMessage reuse the exact same ImmutableArtifact
trust boundary defined below for Task 12's staged package artifacts --
ordinary create/save/bulk_create/update/bulk_update/delete are blocked
outright for all three. Unlike Task 12's fully-immutable artifacts, each
of these three has a small, explicitly allow-listed subset of fields that
*is* expected to change after creation (credential rotation/revocation,
Outbox delivery state, Inbox processing state) -- never through ordinary
save()/QuerySet.update(), only through the owning service module's private
controlled writer, which calls _write_immutable_row_fields() below.
"""

import uuid
from types import MappingProxyType
from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from linuxlab.core.models import DomainEvent

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


def _insert_immutable_row(model_cls, **fields):
    """Task 14 shared private insertion bypass for ImmutableArtifact
    subclasses (the same pattern as Task 12's own _insert_trusted() in
    services.py, kept as a separate helper here rather than importing that
    one directly -- that one is scoped to Task 12's five models and is
    never touched by Task 14). Construct and force-insert one row,
    bypassing save()'s unconditional rejection via Model.save_base(),
    exactly the call save() itself would make had it not been overridden.
    `model_cls` is never accepted from an external caller -- every call
    site hardcodes one of the owning service module's own models.
    """
    instance = model_cls(**fields)
    models.Model.save_base(instance, force_insert=True)
    return instance


# Task 14 corrective (independent review finding #2): the mutable-field
# allow-list for each protected model is fixed HERE, inside the trust
# boundary, keyed by model class -- never accepted as a parameter from
# a caller. The old signature let any caller pass its own allowed_fields,
# which meant the "allow-list" was only as trustworthy as whichever call
# site constructed it; a model-fixed mapping cannot be widened or
# swapped by a caller, buggy or otherwise.
#
# Second corrective (independent re-review finding #1): _MUTABLE_FIELDS_BY_MODEL
# is defined once, in full, near the bottom of this module (after every
# protected model class exists) as a types.MappingProxyType over frozenset
# values -- both the mapping itself and each per-model value reject
# mutation outright (item assignment/deletion on a mappingproxy raises
# TypeError; .add() on a frozenset raises AttributeError). The two
# functions below only reference the name at CALL time (ordinary Python
# late-binding for module globals), so they do not need it to already
# hold its final value when they are themselves defined.


def _write_immutable_row_fields(instance, **changes):
    """Update ONLY the model-fixed mutable subset of fields on the ONE
    row `instance` identifies (credential rotation/revocation for
    NodeRegistration; delivery metadata for OutboxMessage; processing
    metadata for InboxMessage), bypassing the guarded QuerySet.update()
    rejection by calling Django's underlying QuerySet.update() directly --
    the same bypass spirit as Task 12's _write_installed_package_lifecycle().
    The allow-list is resolved from `_MUTABLE_FIELDS_BY_MODEL[type(instance)]`,
    never from the caller.
    """
    model_cls = type(instance)
    allowed = _MUTABLE_FIELDS_BY_MODEL[model_cls]
    unexpected = set(changes) - allowed
    if unexpected:
        raise ValueError(
            f"cannot write non-mutable {model_cls.__name__} fields: {sorted(unexpected)}"
        )

    updated = models.QuerySet.update(model_cls.objects.filter(pk=instance.pk), **changes)
    if updated != 1:
        raise ValueError(f"{model_cls.__name__} {instance.pk} does not exist")


def _bulk_write_immutable_rows(queryset, **changes):
    """Batch equivalent of _write_immutable_row_fields(), for updating
    many rows of the same protected model at once (e.g. marking several
    OutboxMessage rows DELIVERED together). `queryset` must already be
    filtered by the caller to the exact target rows; the mutable-field
    allow-list is still resolved from this trust boundary's own
    model-fixed mapping, never from the caller. Returns the number of
    rows actually updated -- 0 is a legitimate, safe outcome (e.g. a
    stale/fenced claim that no longer matches the caller's precondition
    filter), not an error.
    """
    model_cls = queryset.model
    allowed = _MUTABLE_FIELDS_BY_MODEL[model_cls]
    unexpected = set(changes) - allowed
    if unexpected:
        raise ValueError(
            f"cannot write non-mutable {model_cls.__name__} fields: {sorted(unexpected)}"
        )

    return models.QuerySet.update(queryset, **changes)


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


# --- Task 14: Node <-> Central event sync -----------------------------------

OUTBOX_PENDING = "pending"
OUTBOX_IN_FLIGHT = "in_flight"
OUTBOX_DELIVERED = "delivered"
OUTBOX_DEAD_LETTER = "dead_letter"

OUTBOX_STATE_CHOICES = [
    (OUTBOX_PENDING, "Pending"),
    (OUTBOX_IN_FLIGHT, "In flight"),
    (OUTBOX_DELIVERED, "Delivered"),
    (OUTBOX_DEAD_LETTER, "Dead letter"),
]

INBOX_RECEIVED = "received"
INBOX_PROCESSED = "processed"
INBOX_FAILED = "failed"

INBOX_STATE_CHOICES = [
    (INBOX_RECEIVED, "Received"),
    (INBOX_PROCESSED, "Processed"),
    (INBOX_FAILED, "Failed"),
]


REGISTRATION_ACTIVE = "active"
REGISTRATION_SUPERSEDED = "superseded"
REGISTRATION_REVOKED = "revoked"

REGISTRATION_STATE_CHOICES = [
    (REGISTRATION_ACTIVE, "Active"),
    (REGISTRATION_SUPERSEDED, "Superseded"),
    (REGISTRATION_REVOKED, "Revoked"),
]


class NodeRegistration(ImmutableArtifact):
    """Central-issued, Central-owned Node identity and credential state
    (T14-D18, T14-D19, T14-D23, T14-D28). `node_id`/`display_name`/
    `created_at` are permanent identity; `current_secret_hash`,
    `previous_secret_hash`, `previous_valid_until`, `enabled`,
    `rotated_at`, and `revoked_at` are credential-lifecycle state, mutable
    only through linuxlab.sync.authentication's private controlled writer
    -- never through ordinary save()/QuerySet.update(). Never stores
    plaintext; only Django password hashes (T14-D19, T14-D37)."""

    node_id = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    state = models.CharField(
        max_length=10, choices=REGISTRATION_STATE_CHOICES, default=REGISTRATION_REVOKED
    )
    display_name = models.CharField(max_length=255)
    current_secret_hash = models.TextField()
    previous_secret_hash = models.TextField(blank=True, default="")
    previous_valid_until = models.DateTimeField(null=True, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta(ImmutableArtifact.Meta):
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(
                    state__in=[REGISTRATION_ACTIVE, REGISTRATION_SUPERSEDED, REGISTRATION_REVOKED]
                ),
                name="node_registration_state_allowed_values",
            ),
            models.UniqueConstraint(
                fields=["node_id"],
                condition=Q(state=REGISTRATION_ACTIVE),
                name="unique_active_node_registration_per_node",
            ),
        ]


class OutboxMessage(ImmutableArtifact):
    """One Node-local queued sync event (T14-D10, T14-D11, T14-D31). The
    wire envelope fields (event_id through payload) are the immutable
    snapshot captured at enqueue time -- retries resend exactly this, never
    a reserialization of current domain state. Delivery metadata (state
    through created_at) is mutable only through linuxlab.sync.outbox's
    private controlled writer.

    `domain_event` is a trace/audit link only (T14-D11); wire semantics
    never depend on re-reading it. PROTECT mirrors the rest of this app's
    convention of never allowing an ordinary cascade to silently remove
    retained history.
    """

    event_id = models.UUIDField(unique=True, editable=False)
    domain_event = models.ForeignKey(
        DomainEvent,
        on_delete=models.PROTECT,
        related_name="outbox_messages",
        null=True,
        blank=True,
    )
    event_type = models.CharField(max_length=255)
    schema_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    source_node_id = models.UUIDField()
    occurred_at = models.DateTimeField()
    aggregate_type = models.CharField(max_length=100)
    aggregate_public_id = models.UUIDField()
    payload = models.JSONField()

    state = models.CharField(max_length=20, choices=OUTBOX_STATE_CHOICES, default=OUTBOX_PENDING)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    # Corrective (independent review finding #3): a fresh claim_token is
    # minted every time a row is claimed/reclaimed. Every post-claim write
    # (delivered/dead-letter/release) must match this EXACT token, not
    # merely event_id + IN_FLIGHT, so a worker whose lease already expired
    # and was reclaimed by another worker can never affect the row again --
    # its write matches zero rows instead.
    claim_token = models.UUIDField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    rejection_code = models.CharField(max_length=100, blank=True, default="")
    rejection_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(ImmutableArtifact.Meta):
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(
                    state__in=[
                        OUTBOX_PENDING,
                        OUTBOX_IN_FLIGHT,
                        OUTBOX_DELIVERED,
                        OUTBOX_DEAD_LETTER,
                    ]
                ),
                name="outbox_message_state_allowed_values",
            ),
        ]


class InboxMessage(ImmutableArtifact):
    """Central's durable acceptance ledger (T14-D12, T14-D26, T14-D27).
    Event content fields (event_id through payload) are immutable once
    accepted -- a same-`event_id`-different-content event is a conflict,
    never an overwrite (T14-D27), which is enforced entirely at the
    linuxlab.sync.ingestion service layer since this row's own guard
    already forbids any ordinary rewrite. Processing metadata
    (processing_state, processed_at, processing_error) is mutable only
    through linuxlab.sync.processing's private controlled writer, and
    never affects Node ACK/delivery semantics (T14-D12).
    """

    event_id = models.UUIDField(unique=True, editable=False)
    source_node = models.ForeignKey(
        NodeRegistration, on_delete=models.PROTECT, related_name="inbox_messages"
    )
    event_type = models.CharField(max_length=255)
    schema_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    occurred_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)
    aggregate_type = models.CharField(max_length=100)
    aggregate_public_id = models.UUIDField()
    payload = models.JSONField()

    processing_state = models.CharField(
        max_length=20, choices=INBOX_STATE_CHOICES, default=INBOX_RECEIVED
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    processing_error = models.TextField(blank=True, default="")

    class Meta(ImmutableArtifact.Meta):
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(processing_state__in=[INBOX_RECEIVED, INBOX_PROCESSED, INBOX_FAILED]),
                name="inbox_message_processing_state_allowed_values",
            ),
        ]


# Task 14 corrective: the ONE fixed source of truth for which fields
# _write_immutable_row_fields()/_bulk_write_immutable_rows() may ever
# write on each protected model -- defined here, after every class
# above is defined, rather than accepted from any caller.
#
# Second corrective: a MappingProxyType over frozenset values, built in
# one shot and never mutated afterward. `_MUTABLE_FIELDS_BY_MODEL[X] = ...`
# and `del _MUTABLE_FIELDS_BY_MODEL[X]` both raise TypeError (mappingproxy
# rejects item assignment/deletion); `_MUTABLE_FIELDS_BY_MODEL[X].add(...)`
# raises AttributeError (frozenset has no add()). There is no public name
# anywhere in this module bound to the underlying plain dict -- the dict
# literal is constructed and wrapped in the same expression, so nothing
# outside this statement ever holds a mutable reference to it.
_MUTABLE_FIELDS_BY_MODEL = MappingProxyType(
    {
        NodeRegistration: frozenset(
            {
                "current_secret_hash",
                "previous_secret_hash",
                "previous_valid_until",
                "enabled",
                "rotated_at",
                "revoked_at",
            }
        ),
        OutboxMessage: frozenset(
            {
                "state",
                "attempts",
                "next_attempt_at",
                "claimed_at",
                "lease_until",
                "claim_token",
                "delivered_at",
                "rejection_code",
                "rejection_reason",
            }
        ),
        InboxMessage: frozenset(
            {
                "processing_state",
                "processed_at",
                "processing_error",
            }
        ),
    }
)
