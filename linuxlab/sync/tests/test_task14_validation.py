"""Task 14 corrective: complete inbound envelope validation and UTC
timestamp normalization (independent review findings #5 and #6).

normalize_wire_event() is what stands between an arbitrary/poison
inbound envelope and any database insert -- every check here exists
specifically to prevent something that would otherwise only surface as
a raw DB exception (an oversized CharField) or a silent correctness bug
(a spoofed aggregate identity, a naive timestamp breaking exact-replay
duplicate detection).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from linuxlab.sync.contracts import (
    WireEventEnvelope,
    WireEventValidationError,
    normalize_wire_event,
)
from linuxlab.sync.enqueue import ACTIVITY_COMPLETED_V1


def _make_envelope(**overrides):
    attempt_public_id = uuid.uuid4()
    fields = {
        "event_id": uuid.uuid4(),
        "event_type": "activity.completed",
        "occurred_at": datetime.now(UTC),
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


def _normalize(**overrides):
    return normalize_wire_event(_make_envelope(**overrides), ACTIVITY_COMPLETED_V1)


# --- happy path ----------------------------------------------------------


def test_valid_envelope_normalizes_successfully():
    normalized = _normalize()

    assert normalized.event_type == "activity.completed"
    assert normalized.aggregate_type == "attempt"


# --- bounded strings -------------------------------------------------------


def test_aggregate_type_longer_than_model_contract_is_rejected():
    with pytest.raises(WireEventValidationError):
        _normalize(aggregate_type="x" * 101)


def test_event_type_longer_than_model_contract_is_rejected():
    with pytest.raises(WireEventValidationError):
        _normalize(event_type="x" * 256)


# --- wrong / malformed aggregate identity -----------------------------------


def test_wrong_aggregate_type_is_rejected():
    with pytest.raises(WireEventValidationError):
        _normalize(aggregate_type="not-an-attempt")


def test_malformed_aggregate_public_id_is_rejected():
    with pytest.raises(WireEventValidationError):
        _normalize(aggregate_public_id="not-a-uuid")


def test_aggregate_public_id_mismatch_with_payload_attempt_id_is_rejected():
    with pytest.raises(WireEventValidationError):
        _normalize(aggregate_public_id=uuid.uuid4())  # payload's attempt_public_id differs


# --- payload UUID identities -------------------------------------------------


@pytest.mark.parametrize(
    "key", ["attempt_public_id", "learner_public_id", "activity_definition_public_id"]
)
def test_payload_uuid_identity_fields_that_are_not_uuids_are_rejected(key):
    envelope = _make_envelope()
    tampered_payload = dict(envelope.payload)
    tampered_payload[key] = "not-a-uuid"
    with pytest.raises(WireEventValidationError):
        normalize_wire_event(
            WireEventEnvelope(
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                occurred_at=envelope.occurred_at,
                source_node_id=envelope.source_node_id,
                aggregate_type=envelope.aggregate_type,
                aggregate_public_id=envelope.aggregate_public_id,
                schema_version=envelope.schema_version,
                payload=tampered_payload,
            ),
            ACTIVITY_COMPLETED_V1,
        )


# --- schema version -----------------------------------------------------


def test_invalid_schema_version_is_rejected():
    with pytest.raises(WireEventValidationError):
        _normalize(schema_version=0)

    with pytest.raises(WireEventValidationError):
        _normalize(schema_version="1")


# --- timestamp normalization / naive rejection ------------------------------


def test_naive_timestamp_is_rejected():
    naive = datetime(2026, 8, 31, 10, 0, 0)  # noqa: DTZ001 -- intentionally naive
    with pytest.raises(WireEventValidationError):
        _normalize(occurred_at=naive)


def test_aware_timestamp_is_normalized_to_utc():
    from datetime import timezone as dt_timezone

    plus_seven = datetime(2026, 8, 31, 10, 0, 0, tzinfo=dt_timezone(timedelta(hours=7)))

    normalized = _normalize(occurred_at=plus_seven)

    assert normalized.occurred_at == datetime(2026, 8, 31, 3, 0, 0, tzinfo=UTC)
    assert normalized.occurred_at.tzinfo == UTC


def test_equivalent_instants_in_different_offsets_normalize_identically():
    from datetime import timezone as dt_timezone

    as_utc = datetime(2026, 8, 31, 3, 0, 0, tzinfo=UTC)
    as_plus_seven = datetime(2026, 8, 31, 10, 0, 0, tzinfo=dt_timezone(timedelta(hours=7)))
    as_zero_offset = datetime(2026, 8, 31, 3, 0, 0, tzinfo=UTC)

    normalized_1 = _normalize(occurred_at=as_utc)
    normalized_2 = _normalize(occurred_at=as_plus_seven)
    normalized_3 = _normalize(occurred_at=as_zero_offset)

    assert normalized_1.occurred_at == normalized_2.occurred_at == normalized_3.occurred_at
