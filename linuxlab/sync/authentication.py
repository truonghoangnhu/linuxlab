"""Task 14 Node credential lifecycle (T14-D18, T14-D19, T14-D23, T14-D28,
T14-D37).

Central provisions immutable Node UUID identity and opaque high-entropy
bearer secrets, verified through Django's own password-hasher framework
-- never JWT/OAuth, never a recoverable encrypted plaintext store, never
Task 11 package signing keys, never Django SECRET_KEY. Plaintext exists
only in the return value of issue_node_registration()/
rotate_node_credential() -- it is never persisted by Central at all.
"""

import secrets
from dataclasses import dataclass, field
from uuid import UUID

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone

from .models import NodeRegistration, _insert_immutable_row, _write_immutable_row_fields


class NodeAuthenticationError(Exception):
    """Raised for every authentication failure with the same generic
    message, deliberately: an unknown node_id, a disabled/revoked
    registration, and a wrong secret must be indistinguishable to the
    caller (do not leak which one occurred)."""


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


def issue_node_registration(display_name):
    """Provision a new Central-issued immutable Node UUID identity with a
    fresh opaque bearer secret (T14-D18, T14-D23). No self-service
    enrollment: this is a controlled administrative operation only."""
    secret = _generate_secret()
    registration = _insert_immutable_row(
        NodeRegistration,
        display_name=display_name,
        current_secret_hash=make_password(secret),
        previous_secret_hash="",
        previous_valid_until=None,
        enabled=True,
    )
    return IssuedNodeCredential(node_id=registration.node_id, secret=secret)


def rotate_node_credential(node_id, grace_period):
    """Rotate a Node's bearer secret: the new secret becomes current
    immediately, the old current secret becomes previous and remains
    valid only until `now + grace_period` (T14-D28)."""
    registration = NodeRegistration.objects.get(node_id=node_id)
    secret = _generate_secret()
    now = timezone.now()
    _write_immutable_row_fields(
        registration,
        current_secret_hash=make_password(secret),
        previous_secret_hash=registration.current_secret_hash,
        previous_valid_until=now + grace_period,
        rotated_at=now,
    )
    return IssuedNodeCredential(node_id=node_id, secret=secret)


def revoke_node_registration(node_id):
    """Emergency revoke: disables current AND previous credentials
    immediately (T14-D28). Setting `enabled=False` alone is sufficient --
    authenticate_node() checks `enabled` before comparing any secret, so
    a disabled registration can never authenticate via either hash."""
    registration = NodeRegistration.objects.get(node_id=node_id)
    _write_immutable_row_fields(
        registration,
        enabled=False,
        revoked_at=timezone.now(),
    )


def authenticate_node(node_id, bearer_secret, now=None):
    """Verify a Node's bearer secret against its current or (within
    grace) previous stored hash, and return the authenticated
    NodeRegistration -- the sole trusted source of Node identity
    (T14-D07, T14-D37). Raises NodeAuthenticationError uniformly for
    every failure mode."""
    now = now or timezone.now()
    try:
        registration = NodeRegistration.objects.get(node_id=node_id)
    except NodeRegistration.DoesNotExist as exc:
        raise NodeAuthenticationError("authentication failed") from exc

    if not registration.enabled:
        raise NodeAuthenticationError("authentication failed")

    if check_password(bearer_secret, registration.current_secret_hash):
        return registration

    if (
        registration.previous_secret_hash
        and registration.previous_valid_until is not None
        and now <= registration.previous_valid_until
        and check_password(bearer_secret, registration.previous_secret_hash)
    ):
        return registration

    raise NodeAuthenticationError("authentication failed")
