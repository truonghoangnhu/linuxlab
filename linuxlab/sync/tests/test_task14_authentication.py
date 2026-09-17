"""Node credential lifecycle: issue/rotate/revoke/authenticate (T14-D18,
T14-D19, T14-D23, T14-D28, T14-D37; Corrective B Task 5: active-only
lifecycle authority, no grace window). Central never stores or returns
plaintext; only Django password hashes."""

import io
import uuid
from datetime import UTC, datetime

import pytest
from django.core.management import CommandError, call_command

from linuxlab.sync.authentication import (
    IssuedNodeCredential,
    NodeAuthenticationError,
    NodeRegistrationError,
    NodeRegistrationInvariantError,
    authenticate_node,
    get_active_node_registration,
    issue_node_registration,
    registration_advisory_key,
    revoke_node_registration,
    rotate_node_credential,
)
from linuxlab.sync.models import (
    INBOX_RECEIVED,
    REGISTRATION_ACTIVE,
    REGISTRATION_REVOKED,
    InboxMessage,
    NodeRegistration,
    _insert_immutable_row,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _central_sync_authority(settings):
    """The manage_sync_node CLI tests below need Central's provisioning
    capability enabled to test their own (non-profile) failure modes;
    profile gating itself is proven independently in
    test_task14_profiles.py."""
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True


def test_issue_returns_plaintext_secret_and_stores_only_a_hash():
    credential = issue_node_registration("Lab A")

    assert isinstance(credential, IssuedNodeCredential)
    assert isinstance(credential.secret, str)
    assert len(credential.secret) >= 32

    registration = NodeRegistration.objects.get(node_id=credential.node_id)
    assert registration.state == REGISTRATION_ACTIVE
    assert registration.current_secret_hash != credential.secret
    assert credential.secret not in repr(registration)
    assert credential.secret not in registration.current_secret_hash


# --- corrective (independent review finding #8): the plaintext secret must
# never appear in IssuedNodeCredential's own repr(), not just the DB row's.


def test_issued_credential_repr_excludes_secret():
    credential = issue_node_registration("Lab A")

    assert credential.secret not in repr(credential)
    assert "secret=" not in repr(credential)


def test_rotated_credential_repr_excludes_secret():
    credential = issue_node_registration("Lab A")

    rotated = rotate_node_credential(credential.node_id)

    assert rotated.secret not in repr(rotated)
    assert "secret=" not in repr(rotated)


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


def test_revoked_registration_fails_with_the_same_generic_error():
    credential = issue_node_registration("Lab A")
    revoke_node_registration(credential.node_id)

    with pytest.raises(NodeAuthenticationError) as excinfo_revoked:
        authenticate_node(credential.node_id, credential.secret)

    with pytest.raises(NodeAuthenticationError) as excinfo_unknown:
        authenticate_node(uuid.uuid4(), "whatever")

    assert str(excinfo_revoked.value) == str(excinfo_unknown.value)


def test_superseded_registration_fails_with_the_same_generic_error():
    credential = issue_node_registration("Lab A")
    old_secret = credential.secret
    rotate_node_credential(credential.node_id)

    with pytest.raises(NodeAuthenticationError) as excinfo_superseded:
        authenticate_node(credential.node_id, old_secret)

    with pytest.raises(NodeAuthenticationError) as excinfo_unknown:
        authenticate_node(uuid.uuid4(), "whatever")

    assert str(excinfo_superseded.value) == str(excinfo_unknown.value)


def test_zero_active_scope_after_revoke_fails_generically():
    credential = issue_node_registration("Lab A")
    revoke_node_registration(credential.node_id)

    with pytest.raises(NodeRegistrationError):
        get_active_node_registration(credential.node_id)
    with pytest.raises(NodeAuthenticationError):
        authenticate_node(credential.node_id, credential.secret)


def test_malformed_secret_never_authenticates():
    credential = issue_node_registration("Lab A")

    for bogus in ("", "   ", "\x00\x01", "a" * 10_000):
        with pytest.raises(NodeAuthenticationError):
            authenticate_node(credential.node_id, bogus)


def test_registration_row_never_created_through_ordinary_orm():
    # Sanity check that authentication.py relies on the Task 14 trust
    # boundary in models.py, not a parallel unguarded path.
    with pytest.raises(ValueError, match="cannot be created"):
        NodeRegistration.objects.create(
            node_id=uuid.uuid4(), display_name="x", current_secret_hash="h"
        )


# --- rotation: active-only, no grace window (Corrective B Task 5) ----------


def test_rotation_replaces_row_without_grace():
    credential = issue_node_registration("Lab A")
    before = NodeRegistration.objects.get(node_id=credential.node_id)
    replacement = rotate_node_credential(credential.node_id)
    before.refresh_from_db()
    after = NodeRegistration.objects.get(node_id=credential.node_id, state="active")
    assert after.pk != before.pk
    assert before.state == "superseded"
    assert replacement.node_id == credential.node_id
    with pytest.raises(NodeAuthenticationError):
        authenticate_node(credential.node_id, credential.secret)
    assert authenticate_node(replacement.node_id, replacement.secret).pk == after.pk


def test_rotate_unknown_node_fails():
    with pytest.raises(NodeRegistrationError):
        rotate_node_credential(uuid.uuid4())


def test_rotate_after_revoke_fails_with_zero_active():
    credential = issue_node_registration("Lab A")
    revoke_node_registration(credential.node_id)

    with pytest.raises(NodeRegistrationError):
        rotate_node_credential(credential.node_id)


def test_revoke_disables_the_active_registration_immediately():
    credential = issue_node_registration("Lab A")

    revoke_node_registration(credential.node_id)

    with pytest.raises(NodeAuthenticationError):
        authenticate_node(credential.node_id, credential.secret)
    registration = NodeRegistration.objects.get(node_id=credential.node_id)
    assert registration.state == REGISTRATION_REVOKED
    assert registration.revoked_at is not None


def test_revoke_unknown_node_fails():
    with pytest.raises(NodeRegistrationError):
        revoke_node_registration(uuid.uuid4())


def test_revoke_twice_fails_the_second_time():
    credential = issue_node_registration("Lab A")
    revoke_node_registration(credential.node_id)

    with pytest.raises(NodeRegistrationError):
        revoke_node_registration(credential.node_id)


def test_issue_against_existing_active_scope_fails_without_mutation():
    credential = issue_node_registration("Lab A")

    with pytest.raises(NodeRegistrationError):
        issue_node_registration("Lab A replacement", node_id=credential.node_id)

    assert NodeRegistration.objects.filter(node_id=credential.node_id).count() == 1
    authenticate_node(credential.node_id, credential.secret)


def test_issue_with_explicit_node_id_reprovisions_a_zero_active_scope():
    credential = issue_node_registration("Lab A")
    revoke_node_registration(credential.node_id)

    replacement = issue_node_registration("Lab A reissued", node_id=credential.node_id)

    assert replacement.node_id == credential.node_id
    authenticate_node(replacement.node_id, replacement.secret)
    with pytest.raises(NodeAuthenticationError):
        authenticate_node(credential.node_id, credential.secret)


def test_issue_never_accepts_a_non_uuid_node_id():
    with pytest.raises(NodeRegistrationError):
        issue_node_registration("Lab A", node_id="not-a-uuid")


def test_rotate_and_revoke_never_accept_a_non_uuid_node_id():
    with pytest.raises(NodeRegistrationError):
        rotate_node_credential("not-a-uuid")
    with pytest.raises(NodeRegistrationError):
        revoke_node_registration("not-a-uuid")


def test_provisioning_disabled_by_profile_rejects_every_writer(settings):
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False

    with pytest.raises(NodeRegistrationError):
        issue_node_registration("Lab A")
    with pytest.raises(NodeRegistrationError):
        rotate_node_credential(uuid.uuid4())
    with pytest.raises(NodeRegistrationError):
        revoke_node_registration(uuid.uuid4())


# --- get_active_node_registration / registration_advisory_key --------------


def test_get_active_node_registration_raises_on_zero_active():
    with pytest.raises(NodeRegistrationError):
        get_active_node_registration(uuid.uuid4())


def test_get_active_node_registration_raises_invariant_error_via_stubbed_read(monkeypatch):
    node_id = uuid.uuid4()
    stub_rows = [
        NodeRegistration(
            node_id=node_id,
            state=REGISTRATION_ACTIVE,
            display_name="a",
            current_secret_hash="h1",
        ),
        NodeRegistration(
            node_id=node_id,
            state=REGISTRATION_ACTIVE,
            display_name="b",
            current_secret_hash="h2",
        ),
    ]

    def _fake_filter(*args, **kwargs):
        del args, kwargs
        return stub_rows

    monkeypatch.setattr(NodeRegistration.objects, "filter", _fake_filter)

    with pytest.raises(NodeRegistrationInvariantError):
        get_active_node_registration(node_id)


def test_registration_advisory_key_is_deterministic_and_namespaced():
    node_id = uuid.uuid4()

    assert registration_advisory_key(node_id) == registration_advisory_key(node_id)
    assert isinstance(registration_advisory_key(node_id), int)
    # Fits PostgreSQL's signed bigint domain.
    assert -(2**63) <= registration_advisory_key(node_id) < 2**63


def test_registration_advisory_key_differs_for_different_nodes():
    assert registration_advisory_key(uuid.uuid4()) != registration_advisory_key(uuid.uuid4())


# --- Inbox foreign key preserved across lifecycle transitions --------------


def test_rotation_preserves_existing_inbox_foreign_key():
    credential = issue_node_registration("Lab Inbox")
    registration = NodeRegistration.objects.get(
        node_id=credential.node_id, state=REGISTRATION_ACTIVE
    )
    inbox = _insert_immutable_row(
        InboxMessage,
        event_id=uuid.uuid4(),
        source_node=registration,
        event_type="activity.completed",
        schema_version=1,
        occurred_at=datetime.now(UTC),
        aggregate_type="attempt",
        aggregate_public_id=uuid.uuid4(),
        payload={"score": 1, "max_score": 1},
        processing_state=INBOX_RECEIVED,
    )

    rotate_node_credential(credential.node_id)

    refreshed_inbox = InboxMessage.objects.get(pk=inbox.pk)
    assert refreshed_inbox.source_node_id == registration.pk
    assert refreshed_inbox.source_node.node_id == credential.node_id


# --- insertion rollback and multi-backend races are covered in
# test_registration_concurrency.py (insertion IntegrityError rollback,
# rotate/revoke/issue lock races, and the multiple-ACTIVE invariant).


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
    assert registration.state == REGISTRATION_ACTIVE


def test_manage_sync_node_issue_with_explicit_node_id():
    node_id = uuid.uuid4()
    out = io.StringIO()
    call_command(
        "manage_sync_node", "issue", "--name", "Lab A", "--node-id", str(node_id), stdout=out
    )

    assert str(node_id) in out.getvalue()
    registration = NodeRegistration.objects.get(node_id=node_id)
    assert registration.state == REGISTRATION_ACTIVE


def test_manage_sync_node_rotate_and_revoke_round_trip():
    issue_out = io.StringIO()
    call_command("manage_sync_node", "issue", "--name", "Lab B", stdout=issue_out)
    node_id = issue_out.getvalue().splitlines()[0].split("node_id:", 1)[1].strip()

    rotate_out = io.StringIO()
    call_command("manage_sync_node", "rotate", node_id, stdout=rotate_out)
    assert "secret:" in rotate_out.getvalue()

    revoke_out = io.StringIO()
    call_command("manage_sync_node", "revoke", node_id, stdout=revoke_out)
    assert "revoked" in revoke_out.getvalue()

    with pytest.raises(NodeRegistrationError):
        get_active_node_registration(uuid.UUID(node_id))


def test_manage_sync_node_rotate_unknown_node_raises_command_error():
    with pytest.raises(CommandError):
        call_command("manage_sync_node", "rotate", str(uuid.uuid4()), stdout=io.StringIO())


def test_manage_sync_node_revoke_unknown_node_raises_command_error():
    with pytest.raises(CommandError):
        call_command("manage_sync_node", "revoke", str(uuid.uuid4()), stdout=io.StringIO())


def test_manage_sync_node_rotate_rejects_non_uuid_node_id():
    with pytest.raises(CommandError):
        call_command("manage_sync_node", "rotate", "not-a-uuid", stdout=io.StringIO())


def test_manage_sync_node_grace_hours_flag_no_longer_exists():
    with pytest.raises(CommandError):
        call_command(
            "manage_sync_node",
            "rotate",
            str(uuid.uuid4()),
            "--grace-hours",
            "1",
            stdout=io.StringIO(),
        )
