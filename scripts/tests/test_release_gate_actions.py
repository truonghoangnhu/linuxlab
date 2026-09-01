"""Task 15 Task 7: profile-scoped application action bridge (T15-D05,
T15-D17).

Proves scripts.release_gate.actions correctly orchestrates existing
public/domain services -- Question/ActivityDefinition authoring,
create_question_version()/publish_question_version(),
create_activity_version()/publish_activity_version(),
publish_learning_path(), build_package(), import_package(),
activate_package(), issue_node_registration() -- with no raw ORM/SQL
bypass of trusted state, and that the locked assessment/single_choice
provider works correctly end to end through it.

Runs against pytest-django's own test database: proving the BRIDGE
FUNCTIONS' correctness (right calls, right order, right public/domain
boundary usage). Task 8 proves the same functions work across a real,
separate dual PostgreSQL/Gunicorn deployment.
"""

import json
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.utils import timezone

from linuxlab.sync.authentication import authenticate_node
from linuxlab.sync.contracts import ACCEPTED, DUPLICATE, WireEventEnvelope
from linuxlab.sync.ingestion import ingest_event
from linuxlab.sync.models import InboxMessage, OutboxMessage
from scripts.release_gate.actions import (
    AuthoredSingleChoiceContent,
    author_and_publish_single_choice_activity,
    build_and_sign_package,
    count_inbox_messages,
    find_inbox_message,
    find_outbox_message,
    issue_node_credential,
    republish_learning_path,
    verify_import_and_activate_package,
)

pytestmark = pytest.mark.django_db(transaction=True)

_CONTENT = {
    "prompt": "Which command lists files?",
    "options": [{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
}


def _author():
    return author_and_publish_single_choice_activity(
        prompt=_CONTENT["prompt"],
        options=_CONTENT["options"],
        correct_option_id="a",
    )


# --- Central authoring/publishing ------------------------------------------


def test_author_and_publish_returns_stable_public_identities():
    authored = _author()

    assert isinstance(authored, AuthoredSingleChoiceContent)
    assert isinstance(authored.source_public_id, uuid.UUID)
    assert isinstance(authored.activity_public_id, uuid.UUID)
    assert isinstance(authored.question_public_id, uuid.UUID)
    assert authored.package_version == 1
    assert authored.activity_version == 1
    assert authored.question_version == 1


def test_author_and_publish_result_carries_no_answer_key_material():
    authored = _author()

    # The result is a small dataclass of identities/version numbers plus
    # the snapshot object -- but the snapshot's own JSON payload is the
    # thing an evidence/log writer might serialize, so that is what must
    # never carry answer_key content.
    serialized = json.dumps(authored.snapshot.payload)
    assert "answer_key" not in serialized
    assert "correct_option_id" not in serialized


def test_republish_learning_path_produces_a_new_distinct_version():
    authored = _author()

    second_snapshot = republish_learning_path(authored.source_public_id)

    assert second_snapshot.source_public_id == authored.source_public_id
    assert second_snapshot.version == authored.snapshot.version + 1


# --- package build/sign + verify/import/activate ---------------------------


def test_build_sign_verify_import_activate_round_trips(settings):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    authored = _author()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    package_bytes = build_and_sign_package(authored.snapshot, private_key)
    activated = verify_import_and_activate_package(package_bytes, public_key)

    assert activated.source_public_id == authored.source_public_id
    assert activated.package_version == authored.package_version
    assert activated.state == "active"


def test_verify_import_rejects_a_tampered_package(settings):
    import io
    import zipfile

    from linuxlab.publication.package import SIGNATURE_MEMBER
    from linuxlab.sync.services import PackageImportError

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    authored = _author()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    package_bytes = build_and_sign_package(authored.snapshot, private_key)

    # Corrupt exactly the signature member (never raw/arbitrary byte
    # flipping, which can land inside DEFLATE-compressed member data and
    # raise a zip-level decompression error before signature
    # verification is ever reached): flip one byte of the real Ed25519
    # signature, rewriting only that one archive member.
    reader = zipfile.ZipFile(io.BytesIO(package_bytes))
    members = {name: reader.read(name) for name in reader.namelist()}
    signature = bytearray(members[SIGNATURE_MEMBER])
    signature[0] ^= 0xFF
    members[SIGNATURE_MEMBER] = bytes(signature)

    tampered_buffer = io.BytesIO()
    with zipfile.ZipFile(tampered_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as writer:
        for name, data in members.items():
            writer.writestr(name, data)
    tampered_bytes = tampered_buffer.getvalue()

    with pytest.raises(PackageImportError):
        verify_import_and_activate_package(tampered_bytes, public_key)


def test_build_and_sign_package_uses_the_production_default_registry():
    """The locked provider (assessment/single_choice) must round-trip
    through the SAME registry ordinary application code builds -- never a
    release-gate-only or test-only registry -- proven by successfully
    packaging content whose activity type_key is "assessment" without the
    bridge constructing or injecting any provider itself."""
    authored = _author()
    private_key = Ed25519PrivateKey.generate()

    # No error means build_package() successfully resolved the
    # "assessment" provider from build_default_activity_registry()
    # internally, exactly as the real release path will.
    package_bytes = build_and_sign_package(authored.snapshot, private_key)
    assert len(package_bytes) > 0


# --- Node credential issuance -----------------------------------------------


def test_issue_node_credential_returns_an_authenticatable_credential(settings):
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True

    credential = issue_node_credential("Release gate test node")

    registration = authenticate_node(credential.node_id, credential.secret)
    assert registration.node_id == credential.node_id
    assert registration.enabled is True


# --- read-only Inbox/Outbox inspection --------------------------------------


def _authenticated_node(settings):
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_credential("Inbox inspection test node")
    return authenticate_node(credential.node_id, credential.secret), credential


def _envelope(node_id, event_id=None):
    # aggregate_public_id must match payload["attempt_public_id"] exactly
    # -- the real activity.completed contract (T14-D06) validates this,
    # see enqueue._activity_completed_payload_aggregate_public_id().
    attempt_public_id = uuid.uuid4()
    return WireEventEnvelope(
        event_id=event_id or uuid.uuid4(),
        event_type="activity.completed",
        occurred_at=timezone.now(),
        source_node_id=node_id,
        aggregate_type="attempt",
        aggregate_public_id=attempt_public_id,
        schema_version=1,
        payload={
            "attempt_public_id": str(attempt_public_id),
            "learner_public_id": str(uuid.uuid4()),
            "activity_definition_public_id": str(uuid.uuid4()),
            "activity_version": 1,
            "score": 1,
            "max_score": 1,
        },
    )


def test_find_inbox_message_returns_none_before_ingestion(settings):
    registration, _credential = _authenticated_node(settings)
    del registration

    assert find_inbox_message(uuid.uuid4()) is None


def test_find_inbox_message_returns_the_real_row_after_ingestion(settings):
    registration, _credential = _authenticated_node(settings)
    envelope = _envelope(registration.node_id)

    ack = ingest_event(registration, envelope)
    assert ack.status == ACCEPTED

    found = find_inbox_message(envelope.event_id)
    assert found is not None
    assert isinstance(found, InboxMessage)
    assert found.event_id == envelope.event_id
    assert count_inbox_messages(envelope.event_id) == 1


def test_count_inbox_messages_stays_one_after_duplicate_ingestion(settings):
    registration, _credential = _authenticated_node(settings)
    envelope = _envelope(registration.node_id)

    first = ingest_event(registration, envelope)
    second = ingest_event(registration, envelope)

    assert first.status == ACCEPTED
    assert second.status == DUPLICATE
    assert count_inbox_messages(envelope.event_id) == 1


def test_find_outbox_message_returns_none_when_absent():
    assert find_outbox_message(uuid.uuid4()) is None


def test_find_outbox_message_returns_the_real_row(settings):
    from linuxlab.core.events import publish_event
    from linuxlab.sync.enqueue import enqueue_sync_event

    node_id = uuid.uuid4()
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(node_id)

    domain_event = publish_event(
        "activity.completed",
        {
            "attempt_public_id": str(uuid.uuid4()),
            "learner_public_id": str(uuid.uuid4()),
            "activity_definition_public_id": str(uuid.uuid4()),
            "activity_version": 1,
            "score": 1,
            "max_score": 1,
        },
        aggregate_id=uuid.uuid4(),
    )
    outbox = enqueue_sync_event(domain_event, node_id=node_id, sync_enabled=True)

    found = find_outbox_message(outbox.event_id)
    assert found is not None
    assert isinstance(found, OutboxMessage)
    assert found.event_id == outbox.event_id
