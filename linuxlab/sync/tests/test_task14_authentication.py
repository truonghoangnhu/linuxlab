"""Task 14 Node credential lifecycle: issue/rotate/revoke/authenticate
(T14-D18, T14-D19, T14-D23, T14-D28, T14-D37). Central never stores or
returns plaintext; only Django password hashes."""

import io
import uuid
from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from linuxlab.sync.authentication import (
    IssuedNodeCredential,
    NodeAuthenticationError,
    authenticate_node,
    issue_node_registration,
    revoke_node_registration,
    rotate_node_credential,
)
from linuxlab.sync.models import NodeRegistration

pytestmark = pytest.mark.django_db


def test_issue_returns_plaintext_secret_and_stores_only_a_hash():
    credential = issue_node_registration("Lab A")

    assert isinstance(credential, IssuedNodeCredential)
    assert isinstance(credential.secret, str)
    assert len(credential.secret) >= 32

    registration = NodeRegistration.objects.get(node_id=credential.node_id)
    assert registration.current_secret_hash != credential.secret
    assert credential.secret not in repr(registration)
    assert credential.secret not in registration.current_secret_hash


def test_issued_secret_authenticates():
    credential = issue_node_registration("Lab A")

    registration = authenticate_node(credential.node_id, credential.secret)

    assert registration.node_id == credential.node_id


def test_wrong_secret_fails():
    credential = issue_node_registration("Lab A")

    with pytest.raises(NodeAuthenticationError):
        authenticate_node(credential.node_id, "definitely-wrong-secret")


def test_unknown_node_id_fails_with_the_same_generic_error():
    with pytest.raises(NodeAuthenticationError) as excinfo_unknown:
        authenticate_node(uuid.uuid4(), "whatever")

    credential = issue_node_registration("Lab A")
    with pytest.raises(NodeAuthenticationError) as excinfo_wrong:
        authenticate_node(credential.node_id, "wrong")

    # Same error message regardless of *why* -- does not leak whether the
    # node_id exists or the secret was merely wrong.
    assert str(excinfo_unknown.value) == str(excinfo_wrong.value)


def test_disabled_registration_fails():
    credential = issue_node_registration("Lab A")
    revoke_node_registration(credential.node_id)

    with pytest.raises(NodeAuthenticationError):
        authenticate_node(credential.node_id, credential.secret)


def test_rotation_moves_old_secret_to_previous_with_grace_period():
    credential = issue_node_registration("Lab A")
    old_secret = credential.secret

    new_credential = rotate_node_credential(credential.node_id, timedelta(hours=1))

    assert new_credential.node_id == credential.node_id
    assert new_credential.secret != old_secret

    # New secret works immediately.
    authenticate_node(credential.node_id, new_credential.secret)
    # Old secret still works inside the grace period.
    authenticate_node(credential.node_id, old_secret)


def test_old_secret_fails_outside_grace_period():
    credential = issue_node_registration("Lab A")
    old_secret = credential.secret

    rotate_node_credential(credential.node_id, timedelta(hours=1))

    past_grace = timezone.now() + timedelta(hours=2)
    with pytest.raises(NodeAuthenticationError):
        authenticate_node(credential.node_id, old_secret, now=past_grace)


def test_revoke_disables_both_current_and_previous_immediately():
    credential = issue_node_registration("Lab A")
    old_secret = credential.secret
    new_credential = rotate_node_credential(credential.node_id, timedelta(hours=24))

    revoke_node_registration(credential.node_id)

    with pytest.raises(NodeAuthenticationError):
        authenticate_node(credential.node_id, old_secret)
    with pytest.raises(NodeAuthenticationError):
        authenticate_node(credential.node_id, new_credential.secret)


def test_registration_row_never_created_through_ordinary_orm():
    # Sanity check that authentication.py relies on the Task 14 trust
    # boundary in models.py, not a parallel unguarded path.
    with pytest.raises(ValueError, match="cannot be created"):
        NodeRegistration.objects.create(
            node_id=uuid.uuid4(), display_name="x", current_secret_hash="h"
        )


# --- manage_sync_node command (T14-D23: no self-service enrollment) --------


def test_manage_sync_node_issue_prints_secret_once():
    out = io.StringIO()
    call_command("manage_sync_node", "issue", "--name", "Lab A", stdout=out)

    output = out.getvalue()
    assert "node_id:" in output
    assert "secret:" in output

    node_id = output.splitlines()[0].split("node_id:", 1)[1].strip()
    registration = NodeRegistration.objects.get(node_id=node_id)
    assert registration.display_name == "Lab A"


def test_manage_sync_node_rotate_and_revoke_round_trip():
    issue_out = io.StringIO()
    call_command("manage_sync_node", "issue", "--name", "Lab B", stdout=issue_out)
    node_id = issue_out.getvalue().splitlines()[0].split("node_id:", 1)[1].strip()

    rotate_out = io.StringIO()
    call_command("manage_sync_node", "rotate", node_id, "--grace-hours", "1", stdout=rotate_out)
    assert "secret:" in rotate_out.getvalue()

    revoke_out = io.StringIO()
    call_command("manage_sync_node", "revoke", node_id, stdout=revoke_out)
    assert "revoked" in revoke_out.getvalue()

    registration = NodeRegistration.objects.get(node_id=node_id)
    assert registration.enabled is False


def test_manage_sync_node_rotate_unknown_node_raises_command_error():
    with pytest.raises(CommandError):
        call_command("manage_sync_node", "rotate", str(uuid.uuid4()), stdout=io.StringIO())


def test_manage_sync_node_revoke_unknown_node_raises_command_error():
    with pytest.raises(CommandError):
        call_command("manage_sync_node", "revoke", str(uuid.uuid4()), stdout=io.StringIO())
