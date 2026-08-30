"""Task 14 wire contracts: WireEventEnvelope/EventAck/BatchAck are frozen,
canonicalization is deterministic, and changing any immutable field
changes the canonical bytes (T14-D06)."""

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from linuxlab.sync.contracts import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    BatchAck,
    EventAck,
    WireEventEnvelope,
    canonical_event_bytes,
    canonical_event_dict,
)


def _envelope(**overrides):
    fields = {
        "event_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "event_type": "activity.completed",
        "occurred_at": datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC),
        "source_node_id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "aggregate_type": "attempt",
        "aggregate_public_id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "schema_version": 1,
        "payload": {"score": 1, "max_score": 1},
    }
    fields.update(overrides)
    return WireEventEnvelope(**fields)


def test_wire_event_envelope_is_frozen():
    envelope = _envelope()
    with pytest.raises(FrozenInstanceError):
        envelope.event_type = "other"


def test_canonicalization_is_deterministic():
    first = canonical_event_bytes(_envelope())
    second = canonical_event_bytes(_envelope())
    assert first == second


def test_canonical_dict_contains_exact_wire_fields():
    result = canonical_event_dict(_envelope())
    assert set(result) == {
        "event_id",
        "event_type",
        "occurred_at",
        "source_node_id",
        "aggregate_type",
        "aggregate_public_id",
        "schema_version",
        "payload",
    }
    assert result["event_id"] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.parametrize(
    "field_name,new_value",
    [
        ("event_type", "other.event"),
        ("schema_version", 2),
        ("payload", {"score": 0, "max_score": 1}),
        ("aggregate_public_id", uuid.uuid4()),
        ("occurred_at", datetime(2026, 8, 31, 0, 0, 0, tzinfo=UTC)),
    ],
)
def test_changing_any_field_changes_canonical_bytes(field_name, new_value):
    original = canonical_event_bytes(_envelope())
    changed = canonical_event_bytes(_envelope(**{field_name: new_value}))
    assert original != changed


def test_event_ack_is_frozen_and_carries_status():
    ack = EventAck(event_id=uuid.uuid4(), status=ACCEPTED)
    with pytest.raises(FrozenInstanceError):
        ack.status = REJECTED


def test_event_ack_optional_rejection_fields_default_empty():
    ack = EventAck(event_id=uuid.uuid4(), status=ACCEPTED)
    assert ack.code == ""
    assert ack.reason == ""


def test_batch_ack_status_for_known_event_id():
    event_id = uuid.uuid4()
    batch = BatchAck(results=(EventAck(event_id=event_id, status=DUPLICATE),))
    assert batch.status_for(event_id) == DUPLICATE


def test_batch_ack_status_for_unknown_event_id_is_none_never_delivered():
    batch = BatchAck(results=())
    assert batch.status_for(uuid.uuid4()) is None
