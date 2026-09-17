"""Node credential lifecycle (T14-D18, T14-D19, T14-D23, T14-D28, T14-D37;
Corrective B Task 5: active-only lifecycle authority, no grace window).

Central provisions immutable Node UUID identity and opaque high-entropy
bearer secrets, verified through Django's own password-hasher framework
-- never JWT/OAuth, never a recoverable encrypted plaintext store, never
Task 11 package signing keys, never Django SECRET_KEY. Plaintext exists
only in the return value of issue_node_registration()/
rotate_node_credential() -- it is never persisted by Central at all.

`NodeRegistration.state` (ACTIVE/SUPERSEDED/REVOKED) is the sole runtime
authority. Exactly one ACTIVE row may exist per logical `node_id`
(enforced by a database partial unique constraint); SUPERSEDED is
legitimate rotation history and REVOKED is explicitly invalidated --
neither ever authenticates again, and REVOKED never resurrects. There is
no previous-secret grace window: rotation replaces the ACTIVE row
outright, and the old secret stops working the moment the new one is
committed.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from uuid import UUID

from django.contrib.auth.hashers import (
    check_password,
    identify_hasher,
    is_password_usable,
    make_password,
)
from django.db import connection, transaction
from django.utils import timezone

from .capabilities import can_receive_sync_events
from .models import (
    REGISTRATION_ACTIVE,
    REGISTRATION_REVOKED,
    REGISTRATION_SUPERSEDED,
    NodeRegistration,
    _insert_immutable_row,
    _write_immutable_row_fields,
)

_ADVISORY_LOCK_NAMESPACE = b"linuxlab.sync.registration.v1\0"

_PROVISIONING_DISABLED_MESSAGE = (
    "Node credential provisioning is not enabled on this deployment profile"
)


class NodeAuthenticationError(Exception):
    """Raised for every authentication failure with the same generic
    message, deliberately: an unknown node_id, a REVOKED/SUPERSEDED/
    missing-ACTIVE registration, and a wrong secret must be
    indistinguishable to the caller (do not leak which one occurred)."""


class NodeRegistrationError(ValueError):
    """Raised for every registration-service failure (unknown/zero-ACTIVE
    scope, an already-ACTIVE scope rejecting a new explicit issue,
    disabled provisioning capability, or a malformed node_id) -- these
    are administrative errors, distinct from NodeAuthenticationError."""


class NodeRegistrationInvariantError(NodeRegistrationError):
    """Raised when more than one ACTIVE registration exists for a single
    logical node_id -- a database invariant violation that should be
    unreachable given the partial unique constraint, never a normal
    selection path (no ordering/newest/first() heuristic is used)."""


@dataclass(frozen=True)
class IssuedNodeCredential:
    """The one and only place a plaintext Node secret is ever exposed --
    the return value of issue_node_registration()/rotate_node_credential().
    Central never persists this; the operator/Node stores it locally.
    `secret` is excluded from repr() (corrective, independent review
    finding #8) so it can never leak through a log statement, debugger,
    or exception traceback that happens to print this value object --
    the one authorized place it is still surfaced is the CLI's explicit,
    one-time `self.stdout.write(f"secret: {credential.secret}")`."""

    node_id: UUID
    secret: str = field(repr=False)


def _generate_secret():
    return secrets.token_urlsafe(32)


def _require_node_id(node_id):
    if not isinstance(node_id, UUID):
        raise NodeRegistrationError("node_id must be a UUID")
    return node_id


def _require_provisioning_capability():
    if not can_receive_sync_events():
        raise NodeRegistrationError(_PROVISIONING_DISABLED_MESSAGE)


def registration_advisory_key(node_id: UUID) -> int:
    """Deterministic transaction-scoped advisory lock key for one logical
    node, namespaced independently from publication's own advisory lock
    (linuxlab.publication.snapshots) -- a hash collision only serializes
    two unrelated logical nodes against each other, never merges their
    authority."""
    digest = hashlib.sha256(_ADVISORY_LOCK_NAMESPACE + node_id.bytes).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def lock_registration_scope(node_id: UUID) -> None:
    """Take PostgreSQL's transaction-scoped advisory lock for this
    logical node's registration scope. Must run inside an open
    transaction -- this covers a scope with zero rows, which ordinary
    row locking cannot protect."""
    if not connection.in_atomic_block:
        raise NodeRegistrationError("registration transaction required")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [registration_advisory_key(node_id)])


def get_active_node_registration(node_id: UUID) -> NodeRegistration:
    """Resolve the exactly-one ACTIVE registration for this logical node.
    Zero ACTIVE is a normal (if unusable) outcome; more than one is an
    invariant failure. Never selects by newest timestamp, highest PK/
    version, or arbitrary first()."""
    matches = list(NodeRegistration.objects.filter(node_id=node_id, state=REGISTRATION_ACTIVE))
    if not matches:
        raise NodeRegistrationError(f"no active registration for node {node_id}")
    if len(matches) > 1:
        raise NodeRegistrationInvariantError(f"multiple active registrations for node {node_id}")
    return matches[0]


def registration_is_usable(registration: NodeRegistration) -> bool:
    """Structural usability only: a real UUID identity and a hash string
    Django's password-hasher framework can actually decode -- never
    upgrades/rewrites the hash, and never re-raises a malformed encoding
    with the raw hash value attached."""
    if not isinstance(registration.node_id, UUID):
        return False
    if not is_password_usable(registration.current_secret_hash):
        return False
    try:
        hasher = identify_hasher(registration.current_secret_hash)
        hasher.decode(registration.current_secret_hash)
    except (ValueError, KeyError, IndexError, TypeError):
        return False
    return True


def issue_node_registration(
    display_name: str, *, node_id: UUID | None = None
) -> IssuedNodeCredential:
    """Provision a new Central-issued immutable Node UUID identity with a
    fresh opaque bearer secret (T14-D18, T14-D23). No self-service
    enrollment: this is a controlled administrative operation only.

    `node_id` omitted issues a brand-new logical identity; an explicitly
    supplied `node_id` provisions a new registration only when that
    logical scope currently has zero ACTIVE registrations (the bounded
    explicit remediation after a revoke or an ambiguous migration) -- it
    never reactivates a historical row."""
    _require_provisioning_capability()
    target_node_id = uuid.uuid4() if node_id is None else _require_node_id(node_id)
    secret = _generate_secret()

    with transaction.atomic():
        lock_registration_scope(target_node_id)
        if NodeRegistration.objects.filter(
            node_id=target_node_id, state=REGISTRATION_ACTIVE
        ).exists():
            raise NodeRegistrationError(
                f"node {target_node_id} already has an active registration"
            )
        registration = _insert_immutable_row(
            NodeRegistration,
            node_id=target_node_id,
            display_name=display_name,
            state=REGISTRATION_ACTIVE,
            current_secret_hash=make_password(secret),
            previous_secret_hash="",
            previous_valid_until=None,
            enabled=True,
        )
    return IssuedNodeCredential(node_id=registration.node_id, secret=secret)


def rotate_node_credential(node_id: UUID) -> IssuedNodeCredential:
    """Rotate a Node's bearer secret: requires exactly one usable ACTIVE
    registration, which is superseded, while a brand new row is inserted
    ACTIVE with a fresh secret -- in the same locked transaction, old row
    superseded before the new row is inserted so the partial unique
    constraint is satisfied at every intermediate point. There is no
    grace window: the old secret stops authenticating the instant this
    transaction commits."""
    _require_provisioning_capability()
    _require_node_id(node_id)
    secret = _generate_secret()
    now = timezone.now()

    with transaction.atomic():
        lock_registration_scope(node_id)
        current = get_active_node_registration(node_id)
        _write_immutable_row_fields(current, state=REGISTRATION_SUPERSEDED, rotated_at=now)
        replacement = _insert_immutable_row(
            NodeRegistration,
            node_id=node_id,
            display_name=current.display_name,
            state=REGISTRATION_ACTIVE,
            current_secret_hash=make_password(secret),
            previous_secret_hash="",
            previous_valid_until=None,
            enabled=True,
        )
    return IssuedNodeCredential(node_id=replacement.node_id, secret=secret)


def revoke_node_registration(node_id: UUID) -> None:
    """Emergency revoke: the exactly-one ACTIVE registration for this
    logical node moves to REVOKED, retaining `revoked_at` as evidence.
    Never rotates, re-enables, or creates a replacement -- after this,
    the node has zero ACTIVE authority until an explicit new
    `issue_node_registration(..., node_id=...)` call provisions one."""
    _require_provisioning_capability()
    _require_node_id(node_id)
    now = timezone.now()

    with transaction.atomic():
        lock_registration_scope(node_id)
        current = get_active_node_registration(node_id)
        _write_immutable_row_fields(current, state=REGISTRATION_REVOKED, revoked_at=now)


def authenticate_node(node_id, bearer_secret, now=None):
    """Verify a Node's bearer secret against the current ACTIVE
    registration's stored hash only, and return the authenticated
    NodeRegistration -- the sole trusted source of Node identity
    (T14-D07, T14-D37). Raises NodeAuthenticationError uniformly for
    every failure mode: unknown node_id, zero/multiple ACTIVE, REVOKED/
    SUPERSEDED history, a structurally unusable hash, or a wrong secret.
    `now` is accepted for existing call-site compatibility but grants no
    grace window -- there is no previous-secret authority left to time-
    bound."""
    del now
    try:
        registration = get_active_node_registration(node_id)
    except NodeRegistrationError:
        raise NodeAuthenticationError("authentication failed") from None

    if not registration_is_usable(registration):
        raise NodeAuthenticationError("authentication failed")

    if not check_password(bearer_secret, registration.current_secret_hash):
        raise NodeAuthenticationError("authentication failed")

    return registration
