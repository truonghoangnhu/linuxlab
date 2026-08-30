"""Task 14 durable Central Inbox ingestion and idempotency conflict
detection (T14-D12, T14-D26, T14-D27).
"""

import threading
import uuid
from datetime import UTC, datetime

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from linuxlab.sync.authentication import issue_node_registration
from linuxlab.sync.contracts import ACCEPTED, DUPLICATE, REJECTED, WireEventEnvelope
from linuxlab.sync.ingestion import ingest_event
from linuxlab.sync.models import InboxMessage, NodeRegistration

pytestmark = pytest.mark.django_db


def _make_node():
    credential = issue_node_registration("Test Node")
    return NodeRegistration.objects.get(node_id=credential.node_id)


def _make_envelope(**overrides):
    # aggregate_public_id must equal the payload's own attempt_public_id
    # (normalize_wire_event()'s aggregate/payload consistency check,
    # corrective independent review finding #5) -- exactly how the real
    # enqueue path always produces it.
    attempt_public_id = uuid.uuid4()
    fields = {
        "event_id": uuid.uuid4(),
        "event_type": "activity.completed",
        "occurred_at": timezone.now(),
        "source_node_id": uuid.uuid4(),
        "aggregate_type": "attempt",
        "aggregate_public_id": attempt_public_id,
        "schema_version": 1,
        "payload": {
            "attempt_public_id": str(attempt_public_id),
            "learner_public_id": str(uuid.uuid4()),
            "activity_definition_public_id": str(uuid.uuid4()),
            "activity_version": 1,
            "score": 1,
            "max_score": 1,
        },
    }
    fields.update(overrides)
    return WireEventEnvelope(**fields)


# --- validation rejections ---------------------------------------------


def test_source_node_id_mismatch_is_permanently_rejected():
    node = _make_node()
    envelope = _make_envelope(source_node_id=uuid.uuid4())

    ack = ingest_event(node, envelope)

    assert ack.status == REJECTED
    assert ack.code == "SOURCE_MISMATCH"
    assert InboxMessage.objects.count() == 0


def test_unknown_event_type_is_rejected_as_unsupported_event():
    node = _make_node()
    envelope = _make_envelope(source_node_id=node.node_id, event_type="some.unregistered.event")

    ack = ingest_event(node, envelope)

    assert ack.status == REJECTED
    assert ack.code == "UNSUPPORTED_EVENT"
    assert InboxMessage.objects.count() == 0


def test_unsupported_schema_version_is_rejected():
    node = _make_node()
    envelope = _make_envelope(source_node_id=node.node_id, schema_version=999)

    ack = ingest_event(node, envelope)

    assert ack.status == REJECTED
    assert ack.code == "UNSUPPORTED_SCHEMA"
    assert InboxMessage.objects.count() == 0


def test_invalid_payload_is_rejected():
    node = _make_node()
    envelope = _make_envelope(source_node_id=node.node_id, payload={"bogus": True})

    ack = ingest_event(node, envelope)

    assert ack.status == REJECTED
    assert ack.code == "INVALID_PAYLOAD"
    assert InboxMessage.objects.count() == 0


# --- durable acceptance ---------------------------------------------------


def test_accepted_event_is_durably_committed():
    node = _make_node()
    envelope = _make_envelope(source_node_id=node.node_id)

    ack = ingest_event(node, envelope)

    assert ack.status == ACCEPTED
    row = InboxMessage.objects.get(event_id=envelope.event_id)
    assert row.source_node_id == node.pk
    assert row.event_type == envelope.event_type
    assert row.payload == dict(envelope.payload)


# --- exact duplicate -------------------------------------------------------


def test_exact_duplicate_returns_duplicate_with_one_inbox_row():
    node = _make_node()
    envelope = _make_envelope(source_node_id=node.node_id)

    first = ingest_event(node, envelope)
    second = ingest_event(node, envelope)

    assert first.status == ACCEPTED
    assert second.status == DUPLICATE
    assert InboxMessage.objects.filter(event_id=envelope.event_id).count() == 1


def test_replay_with_different_utc_offset_for_the_same_instant_is_duplicate_not_conflict():
    """Corrective (independent review finding #6): a retry that encodes
    the exact same instant with a different UTC offset (e.g. the Node
    resends with local +07:00 while the first attempt used Z) must
    still be recognized as an exact duplicate, not IDEMPOTENCY_CONFLICT."""
    from datetime import timedelta
    from datetime import timezone as dt_timezone

    node = _make_node()
    as_utc = datetime(2026, 8, 31, 3, 0, 0, tzinfo=UTC)
    as_plus_seven = datetime(2026, 8, 31, 10, 0, 0, tzinfo=dt_timezone(timedelta(hours=7)))
    envelope = _make_envelope(source_node_id=node.node_id, occurred_at=as_utc)
    replay = _make_envelope(
        event_id=envelope.event_id,
        source_node_id=node.node_id,
        occurred_at=as_plus_seven,
        aggregate_public_id=envelope.aggregate_public_id,
        payload=envelope.payload,
    )

    first = ingest_event(node, envelope)
    second = ingest_event(node, replay)

    assert first.status == ACCEPTED
    assert second.status == DUPLICATE
    assert InboxMessage.objects.filter(event_id=envelope.event_id).count() == 1


# --- idempotency conflict ---------------------------------------------------


def test_same_event_id_different_payload_is_idempotency_conflict():
    node = _make_node()
    envelope = _make_envelope(source_node_id=node.node_id)
    ingest_event(node, envelope)

    tampered_payload = dict(envelope.payload)
    tampered_payload["score"] = 0
    tampered = WireEventEnvelope(
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        occurred_at=envelope.occurred_at,
        source_node_id=envelope.source_node_id,
        aggregate_type=envelope.aggregate_type,
        aggregate_public_id=envelope.aggregate_public_id,
        schema_version=envelope.schema_version,
        payload=tampered_payload,
    )

    ack = ingest_event(node, tampered)

    assert ack.status == REJECTED
    assert ack.code == "IDEMPOTENCY_CONFLICT"
    original = InboxMessage.objects.get(event_id=envelope.event_id)
    assert original.payload == dict(envelope.payload)
    assert InboxMessage.objects.filter(event_id=envelope.event_id).count() == 1


# --- real concurrent duplicate ----------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_concurrent_ingest_of_the_same_new_event_id_normalizes_to_one_accepted_one_duplicate():
    credential = issue_node_registration("Concurrent Node")
    node_id = credential.node_id
    envelope = _make_envelope(source_node_id=node_id)

    results = [None, None]
    barrier = threading.Barrier(2)

    class _IngestWorker(threading.Thread):
        def __init__(self, index):
            super().__init__()
            self.index = index

        def run(self):
            close_old_connections()
            try:
                node = NodeRegistration.objects.get(node_id=node_id)
                barrier.wait()
                ack = ingest_event(node, envelope)
                results[self.index] = ack.status
            finally:
                connection.close()

    workers = [_IngestWorker(0), _IngestWorker(1)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sorted(results) == [ACCEPTED, DUPLICATE]
    assert InboxMessage.objects.filter(event_id=envelope.event_id).count() == 1
