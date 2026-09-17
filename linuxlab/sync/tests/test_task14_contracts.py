"""Task 14 wire contracts: WireEventEnvelope/EventAck/BatchAck are frozen,
canonicalization is deterministic, and changing any immutable field
changes the canonical bytes (T14-D06)."""

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from linuxlab.sync.contracts import (
    ACCEPTED,
    AUTH_OR_REGISTRATION_FAILURE,
    DUPLICATE,
    PERMANENT_REJECTION,
    RATE_LIMITED,
    REJECTED,
    SUCCESS,
    TRANSIENT_FAILURE,
    AuthTransportError,
    BatchAck,
    DeliveryFailure,
    EventAck,
    ProtocolTransportError,
    RateLimitedTransportError,
    RequestTooLargeError,
    SyncOutcome,
    TransientTransportError,
    TransportError,
    WireEventEnvelope,
    canonical_event_bytes,
    canonical_event_dict,
    parse_ack_results,
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


# --- Corrective B Task 6: SyncOutcome / DeliveryFailure contract -----------


def test_sync_outcome_has_exactly_the_five_approved_members():
    assert {member.value for member in SyncOutcome} == {
        "SUCCESS",
        "PERMANENT_REJECTION",
        "AUTH_OR_REGISTRATION_FAILURE",
        "TRANSIENT_FAILURE",
        "RATE_LIMITED",
    }


def test_sync_outcome_aliases_are_the_enum_members():
    assert SUCCESS is SyncOutcome.SUCCESS
    assert PERMANENT_REJECTION is SyncOutcome.PERMANENT_REJECTION
    assert AUTH_OR_REGISTRATION_FAILURE is SyncOutcome.AUTH_OR_REGISTRATION_FAILURE
    assert TRANSIENT_FAILURE is SyncOutcome.TRANSIENT_FAILURE
    assert RATE_LIMITED is SyncOutcome.RATE_LIMITED


def test_sync_outcome_is_a_str_enum_and_compares_equal_to_its_string_value():
    assert SyncOutcome.TRANSIENT_FAILURE == "TRANSIENT_FAILURE"
    assert isinstance(SyncOutcome.TRANSIENT_FAILURE, str)


def test_delivery_failure_is_frozen():
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)
    with pytest.raises(FrozenInstanceError):
        failure.category = SyncOutcome.RATE_LIMITED


def test_delivery_failure_rejects_success_as_a_category():
    with pytest.raises(ValueError, match="SUCCESS"):
        DeliveryFailure(category=SyncOutcome.SUCCESS)


def test_delivery_failure_defaults_are_safe_empty_metadata():
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)
    assert failure.http_status is None
    assert failure.error_code == ""
    assert failure.retry_after_seconds is None


@pytest.mark.parametrize("status", [100, 200, 599])
def test_delivery_failure_accepts_valid_http_status_boundaries(status):
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE, http_status=status)
    assert failure.http_status == status


@pytest.mark.parametrize("status", [99, 600, -1])
def test_delivery_failure_rejects_out_of_range_http_status(status):
    with pytest.raises(ValueError, match="http_status"):
        DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE, http_status=status)


def test_delivery_failure_rejects_non_int_http_status():
    with pytest.raises(TypeError, match="http_status"):
        DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE, http_status="400")
    with pytest.raises(TypeError, match="http_status"):
        DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE, http_status=True)


def test_delivery_failure_accepts_int_or_float_retry_after_seconds():
    assert DeliveryFailure(
        category=SyncOutcome.RATE_LIMITED, retry_after_seconds=5
    ).retry_after_seconds == 5
    assert DeliveryFailure(
        category=SyncOutcome.RATE_LIMITED, retry_after_seconds=5.5
    ).retry_after_seconds == 5.5


def test_delivery_failure_rejects_negative_retry_after_seconds():
    with pytest.raises(ValueError, match="retry_after_seconds"):
        DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=-1)


def test_delivery_failure_rejects_nonfinite_retry_after_seconds():
    with pytest.raises(ValueError, match="retry_after_seconds"):
        DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=float("inf"))
    with pytest.raises(ValueError, match="retry_after_seconds"):
        DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=float("nan"))


def test_delivery_failure_rejects_bool_retry_after_seconds():
    with pytest.raises(TypeError, match="retry_after_seconds"):
        DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=True)


def test_delivery_failure_rejects_non_string_error_code():
    with pytest.raises(TypeError, match="error_code"):
        DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE, error_code=None)


# --- Corrective B Task 6: transport error hierarchy -------------------------


def test_transport_error_base_requires_explicit_failure():
    with pytest.raises(TypeError):
        TransportError("transport failure")


def test_transport_error_carries_the_supplied_failure():
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE, error_code="X")
    error = TransportError("boom", failure=failure)
    assert error.failure is failure


def test_transient_transport_error_defaults_to_transient_failure():
    error = TransientTransportError("timeout")
    assert error.failure.category is SyncOutcome.TRANSIENT_FAILURE


def test_auth_transport_error_defaults_to_auth_or_registration_failure():
    error = AuthTransportError("401")
    assert error.failure.category is SyncOutcome.AUTH_OR_REGISTRATION_FAILURE


def test_rate_limited_transport_error_is_a_transient_transport_error():
    error = RateLimitedTransportError("429", retry_after_seconds=30)
    assert isinstance(error, TransientTransportError)
    assert error.failure.category is SyncOutcome.RATE_LIMITED
    assert error.failure.retry_after_seconds == 30


def test_request_too_large_error_defaults_to_transient_with_safe_code():
    error = RequestTooLargeError("413")
    assert isinstance(error, ProtocolTransportError)
    assert error.failure.category is SyncOutcome.TRANSIENT_FAILURE
    assert error.failure.error_code == "BATCH_TOO_LARGE"


# --- Corrective B Task 6 §12: ProtocolTransportError explicit metadata,
# no implicit "protocol error means permanent" fallback ---------------------


def test_protocol_transport_error_requires_explicit_failure():
    with pytest.raises(TypeError):
        ProtocolTransportError("400")


def test_protocol_transport_error_400_is_permanent_rejection():
    error = ProtocolTransportError(
        "request rejected (HTTP 400)",
        failure=DeliveryFailure(category=SyncOutcome.PERMANENT_REJECTION, http_status=400),
    )
    assert error.failure.category is SyncOutcome.PERMANENT_REJECTION
    assert error.failure.http_status == 400


def test_protocol_transport_error_malformed_ack_is_transient_not_permanent():
    """The SAME class (ProtocolTransportError) represents a wholly
    different semantic outcome for a malformed/missing/ambiguous ACK --
    proving there is no implicit class-based "protocol error always
    means permanent" fallback; `.failure.category` alone is authoritative."""
    error = ProtocolTransportError(
        "malformed ACK evidence", failure=DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)
    )
    assert error.failure.category is SyncOutcome.TRANSIENT_FAILURE
    # And this is a DIFFERENT ProtocolTransportError instance/category
    # than the 400 case above -- the class alone never determines it.
    permanent = ProtocolTransportError(
        "400", failure=DeliveryFailure(category=SyncOutcome.PERMANENT_REJECTION, http_status=400)
    )
    assert type(error) is type(permanent)
    assert error.failure.category != permanent.failure.category


# --- Fix round 1 I2: arbitrary-precision integer retry_after_seconds must
# never be routed through math.isfinite()/float() -- Task 8 caps it later,
# Task 6 must not crash or silently truncate it.


def test_delivery_failure_preserves_a_huge_integer_retry_after_exactly():
    huge = 10**400
    failure = DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=huge)
    assert failure.retry_after_seconds == huge
    assert isinstance(failure.retry_after_seconds, int)


def test_delivery_failure_still_rejects_a_negative_huge_integer():
    with pytest.raises(ValueError, match="retry_after_seconds"):
        DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=-(10**400))


def test_delivery_failure_float_retry_after_seconds_still_validated():
    with pytest.raises(ValueError, match="retry_after_seconds"):
        DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=float("inf"))
    with pytest.raises(ValueError, match="retry_after_seconds"):
        DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=-1.5)


# --- Fix round 1 M2: category must be an actual SyncOutcome member, never
# a raw string -- DeliveryFailure never silently coerces.


@pytest.mark.parametrize("bogus", ["SUCCESS", "TRANSIENT_FAILURE", "anything", 1, None])
def test_delivery_failure_rejects_a_non_syncoutcome_category(bogus):
    with pytest.raises((TypeError, ValueError), match="category"):
        DeliveryFailure(category=bogus)


def test_delivery_failure_still_rejects_the_real_success_member():
    with pytest.raises(ValueError, match="SUCCESS"):
        DeliveryFailure(category=SyncOutcome.SUCCESS)


# --- Fix round 1 I4: a structurally malformed per-event `status` (a list,
# dict, or other unhashable value) must never crash the whole parser or
# suppress an independently-valid neighboring event's evidence.


def test_parse_ack_results_list_status_is_event_local_not_a_crash():
    a = uuid.uuid4()
    b = uuid.uuid4()
    raw = [
        {"event_id": str(a), "status": ACCEPTED},
        {"event_id": str(b), "status": []},
    ]

    batch = parse_ack_results(raw)

    assert batch.status_for(a) == ACCEPTED
    assert batch.status_for(b) is None


def test_parse_ack_results_dict_status_is_event_local_not_a_crash():
    a = uuid.uuid4()
    b = uuid.uuid4()
    raw = [
        {"event_id": str(a), "status": ACCEPTED},
        {"event_id": str(b), "status": {}},
    ]

    batch = parse_ack_results(raw)

    assert batch.status_for(a) == ACCEPTED
    assert batch.status_for(b) is None


def test_parse_ack_results_structured_status_neighboring_a_valid_duplicate():
    duplicate_id = uuid.uuid4()
    malformed_id = uuid.uuid4()
    raw = [
        {"event_id": str(duplicate_id), "status": DUPLICATE},
        {"event_id": str(malformed_id), "status": {"nested": True}},
    ]

    batch = parse_ack_results(raw)

    assert batch.status_for(duplicate_id) == DUPLICATE
    assert batch.status_for(malformed_id) is None


def test_parse_ack_results_structured_status_neighboring_a_valid_rejection():
    rejected_id = uuid.uuid4()
    malformed_id = uuid.uuid4()
    raw = [
        {"event_id": str(rejected_id), "status": REJECTED, "code": "IDEMPOTENCY_CONFLICT"},
        {"event_id": str(malformed_id), "status": ["bogus"]},
    ]

    batch = parse_ack_results(raw)

    assert batch.status_for(rejected_id) == REJECTED
    assert batch.status_for(malformed_id) is None


def test_parse_ack_results_structured_status_on_a_duplicated_event_id_still_ambiguous():
    """A malformed structured status is not exempt from the existing
    duplicate-event-id ambiguity rule -- it still counts toward that
    event_id's occurrence count and stays unconfirmed if duplicated."""
    event_id = uuid.uuid4()
    raw = [
        {"event_id": str(event_id), "status": ACCEPTED},
        {"event_id": str(event_id), "status": []},
    ]

    batch = parse_ack_results(raw)

    assert batch.status_for(event_id) is None
