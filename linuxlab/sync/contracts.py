"""Task 14 wire contracts (T14-D06, T14-D20, T14-D22): the stable
normalized envelope crossing Node <-> Central, deliberately separate from
DomainEvent's own persistence shape and from OutboxMessage/InboxMessage's
storage shape. Nothing here is Django-aware.
"""

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

# Matches OutboxMessage/InboxMessage's event_type CharField(max_length=255)
# and aggregate_type CharField(max_length=100) exactly -- validated here,
# BEFORE any insert, so an oversized value never reaches the database as
# an uncaught DataError (corrective, independent review finding #5).
_MAX_EVENT_TYPE_LENGTH = 255
_MAX_AGGREGATE_TYPE_LENGTH = 100

ACCEPTED = "accepted"
DUPLICATE = "duplicate"
REJECTED = "rejected"


@dataclass(frozen=True)
class WireEventEnvelope:
    """The stable normalized fields that actually cross the wire
    (T14-D06). Never a dump of DomainEvent -- callers build this from a
    SyncEventContract's serializer."""

    event_id: UUID
    event_type: str
    occurred_at: datetime
    source_node_id: UUID
    aggregate_type: str
    aggregate_public_id: UUID
    schema_version: int
    payload: Mapping[str, object]


@dataclass(frozen=True)
class EventAck:
    """Per-event ingestion outcome (T14-D08, T14-D30). `code`/`reason`
    are populated only for `rejected`; they must never carry secrets."""

    event_id: UUID
    status: str
    code: str = ""
    reason: str = ""


@dataclass(frozen=True)
class BatchAck:
    """A batch's independent per-event results (T14-D08, T14-D30).
    `status_for()` returns None -- never a delivered/accepted guess --
    for any event_id not explicitly present in the results, so a missing
    or malformed result is always treated as unconfirmed. It also
    returns None for an event_id that appears MORE THAN ONCE in
    `results` (duplicated or conflicting entries) -- ambiguous data can
    never authorize an irreversible action (corrective, independent
    review finding #7)."""

    results: tuple[EventAck, ...] = ()

    def status_for(self, event_id):
        matches = [result.status for result in self.results if result.event_id == event_id]
        if len(matches) != 1:
            return None
        return matches[0]


class SyncTransport(Protocol):
    """T14-D01, T14-D02, T14-D29, T14-D39: the abstraction both
    HttpSyncTransport (production) and FileSyncTransport (dev/test/
    reference only) implement, over the identical WireEventEnvelope."""

    def send_batch(self, envelopes: tuple[WireEventEnvelope, ...]) -> BatchAck: ...


class SyncOutcome(StrEnum):
    """Stable semantic transport/network outcomes (Corrective B Task 6,
    approved spec §12). Retry policy (Task 8) consumes these outcomes --
    it must not inspect HTTP numbers or exception classes; `.category`
    alone is retry authority."""

    SUCCESS = "SUCCESS"
    PERMANENT_REJECTION = "PERMANENT_REJECTION"
    AUTH_OR_REGISTRATION_FAILURE = "AUTH_OR_REGISTRATION_FAILURE"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    RATE_LIMITED = "RATE_LIMITED"


# Upper-case aliases of the enum members, as required by the plan's public
# interface ledger -- exported alongside SyncOutcome itself so callers may
# import either the enum member or the bare constant.
SUCCESS = SyncOutcome.SUCCESS
PERMANENT_REJECTION = SyncOutcome.PERMANENT_REJECTION
AUTH_OR_REGISTRATION_FAILURE = SyncOutcome.AUTH_OR_REGISTRATION_FAILURE
TRANSIENT_FAILURE = SyncOutcome.TRANSIENT_FAILURE
RATE_LIMITED = SyncOutcome.RATE_LIMITED


@dataclass(frozen=True)
class DeliveryFailure:
    """Safe FAILURE metadata only -- never SUCCESS (rejected below), and
    never a raw exception, response body, header, or Authorization value
    (Corrective B Task 6). `error_code` is a short safe machine code
    (e.g. "BATCH_TOO_LARGE"), never a raw remote reason/response."""

    category: SyncOutcome
    http_status: int | None = None
    error_code: str = ""
    retry_after_seconds: int | float | None = None

    def __post_init__(self):
        if self.category is SyncOutcome.SUCCESS:
            raise ValueError("DeliveryFailure cannot represent SUCCESS")
        if self.http_status is not None:
            if isinstance(self.http_status, bool) or not isinstance(self.http_status, int):
                raise TypeError("http_status must be an int or None")
            if not (100 <= self.http_status <= 599):
                raise ValueError("http_status must be a valid HTTP status code")
        if not isinstance(self.error_code, str):
            raise TypeError("error_code must be a string")
        if self.retry_after_seconds is not None:
            if isinstance(self.retry_after_seconds, bool) or not isinstance(
                self.retry_after_seconds, int | float
            ):
                raise TypeError("retry_after_seconds must be an int, float, or None")
            if not math.isfinite(self.retry_after_seconds):
                raise ValueError("retry_after_seconds must be finite")
            if self.retry_after_seconds < 0:
                raise ValueError("retry_after_seconds must be non-negative")


class TransportError(Exception):
    """Base for whole-batch (not per-event) transport failures raised by
    send_batch() -- always classify into exactly one of the subclasses
    below rather than raising this directly (T14-D29). Every instance
    carries `.failure: DeliveryFailure` -- safe metadata only, never a
    raw exception/response body/header (Corrective B Task 6). Retry
    policy consumes `.failure.category`, never this class or the
    message text."""

    def __init__(self, message="transport failure", *, failure):
        super().__init__(message)
        self.failure = failure


class TransientTransportError(TransportError):
    """Timeout, DNS/connect failure, 5xx, or a malformed/unparseable
    response -- always safely retryable; never a reason to dead-letter
    any event (T14-D14). Defaults to TRANSIENT_FAILURE metadata unless
    the caller supplies a more specific `failure` (e.g. RATE_LIMITED via
    the RateLimitedTransportError subclass)."""

    def __init__(self, message="transient transport failure", *, failure=None):
        if failure is None:
            failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)
        super().__init__(message, failure=failure)


class AuthTransportError(TransportError):
    """401/403 -- a channel/credential fault, never an individual event's
    fault; callers must release the whole claimed batch back to PENDING
    without dead-lettering and without incrementing per-event attempts
    (T14-D17). Always AUTH_OR_REGISTRATION_FAILURE metadata."""

    def __init__(self, message="authentication or registration failure", *, failure=None):
        if failure is None:
            failure = DeliveryFailure(category=SyncOutcome.AUTH_OR_REGISTRATION_FAILURE)
        super().__init__(message, failure=failure)


class ProtocolTransportError(TransportError):
    """400 -- the outgoing request itself was malformed; a Task 14
    implementation-side fault, not any one event's fault. The flush
    orchestrator releases the whole batch as retryable (never falsely
    dead-lettering an event over a request-level fault) and flags the
    result as a protocol fault so a deterministic 400 cannot silently
    tight-loop the identical request forever (T14-D40 corrective).

    Requires an EXPLICIT `failure` -- no default category (Corrective B
    Task 6): the same class represents an HTTP 400 (PERMANENT_REJECTION)
    and other response-protocol faults without permanent-rejection
    evidence (TRANSIENT_FAILURE); there is no implicit "protocol error
    means permanent" fallback."""

    def __init__(self, message="protocol failure", *, failure):
        super().__init__(message, failure=failure)


class RequestTooLargeError(ProtocolTransportError):
    """413 -- the outgoing request exceeded Central's accepted batch
    limits. A subclass of ProtocolTransportError so existing generic
    handling still applies if a caller doesn't need the distinction,
    but the flush orchestrator catches this specifically to implement
    T14-D40: split a multi-event batch and retry smaller subsets; a
    singleton event that still receives 413 is permanently unsendable
    under the current transport contract and dead-letters with code
    EVENT_TOO_LARGE. Supplies its own explicit TRANSIENT_FAILURE
    metadata with the safe BATCH_TOO_LARGE code to ProtocolTransportError
    -- callers never need to pass `failure` themselves."""

    def __init__(self, message="request too large", *, failure=None):
        if failure is None:
            failure = DeliveryFailure(
                category=SyncOutcome.TRANSIENT_FAILURE, error_code="BATCH_TOO_LARGE"
            )
        super().__init__(message, failure=failure)


class RateLimitedTransportError(TransientTransportError):
    """429 -- Central is rate-limiting this Node. A subclass of
    TransientTransportError (always safely retryable), carrying
    RATE_LIMITED metadata and, when Central supplied one, a parsed
    Retry-After delay in `.failure.retry_after_seconds`."""

    def __init__(self, message="rate limited", *, failure=None, retry_after_seconds=None):
        if failure is None:
            failure = DeliveryFailure(
                category=SyncOutcome.RATE_LIMITED, retry_after_seconds=retry_after_seconds
            )
        super().__init__(message, failure=failure)


@dataclass(frozen=True)
class SyncEventContract:
    """One registered (event_type, schema_version)'s serialization/
    validation contract (T14-D20, T14-D22).

    `serialize_domain_event(domain_event) -> (aggregate_type, aggregate_public_id, payload)`
    extracts exactly the minimal learner-safe fields for this event type
    -- never a raw DomainEvent.__dict__ dump.

    `validate_payload(payload)` raises ValueError for a structurally
    invalid wire payload; used at Central ingestion before anything is
    persisted.

    `expected_aggregate_type`, if non-empty, is the ONLY aggregate_type
    normalize_wire_event() accepts for this event type -- an envelope
    claiming a different aggregate_type is rejected before insert
    (corrective, independent review finding #5).

    `payload_aggregate_public_id(payload)`, if provided, extracts the
    payload's own canonical aggregate identity (e.g. an attempt's
    public_id) so normalize_wire_event() can reject an envelope whose
    top-level aggregate_public_id doesn't match what its own payload
    claims -- a defense against a tampered/buggy Node, not something
    the legitimate enqueue path can ever produce inconsistently.
    """

    event_type: str
    schema_version: int
    serialize_domain_event: Callable[[object], tuple[str, UUID, Mapping[str, object]]]
    validate_payload: Callable[[Mapping[str, object]], None] = field(
        default=lambda payload: None
    )
    expected_aggregate_type: str = ""
    payload_aggregate_public_id: Callable[[Mapping[str, object]], UUID | None] = field(
        default=lambda payload: None
    )


def canonical_event_dict(envelope):
    return {
        "event_id": str(envelope.event_id),
        "event_type": envelope.event_type,
        "occurred_at": envelope.occurred_at.isoformat(),
        "source_node_id": str(envelope.source_node_id),
        "aggregate_type": envelope.aggregate_type,
        "aggregate_public_id": str(envelope.aggregate_public_id),
        "schema_version": envelope.schema_version,
        "payload": envelope.payload,
    }


def canonical_event_bytes(envelope):
    """Deterministic canonical serialization of ONE event: sorted keys,
    no delimiter ambiguity. Used for immutable-envelope comparison
    (exact duplicate vs idempotency conflict, T14-D27) -- NOT for batch
    byte-size budgeting, since it omits the wrapping-object/separator
    overhead of the actual request body (see serialize_sync_batch())."""
    return json.dumps(
        canonical_event_dict(envelope),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def serialize_sync_batch(envelopes):
    """The ONE shared serializer for a full sync request body -- both
    HttpSyncTransport (what actually goes out over HTTPS) and
    claim_outbox_batch()'s byte-budget accounting use this exact
    function, so the count enforced locally can never mismatch what
    Central actually receives (corrective, independent review finding
    #4). Deterministic: sorted keys, no delimiter ambiguity."""
    return json.dumps(
        {"events": [canonical_event_dict(envelope) for envelope in envelopes]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


class WireEventValidationError(ValueError):
    """Raised by normalize_wire_event() for any structurally or
    semantically invalid inbound envelope. Always maps to a per-event
    REJECTED ACK at Central ingestion -- never a 500, and never an
    uncaught database exception (corrective, independent review finding
    #5): every check here exists specifically to catch, before any
    insert is attempted, something that would otherwise only surface as
    a DB-level failure (an oversized CharField) or a silent data-quality
    problem (a spoofed aggregate identity, a naive timestamp)."""


@dataclass(frozen=True)
class NormalizedWireEvent:
    """The result of normalize_wire_event(): identical shape to
    WireEventEnvelope, except occurred_at is guaranteed UTC-normalized.
    Central persists and compares using ONLY this normalized form, never
    the raw incoming envelope, so two exact-duplicate replays that
    differ only in UTC offset representation (+07:00 vs Z vs +00:00 for
    the same instant) are correctly recognized as the same event
    (corrective, independent review finding #6)."""

    event_id: UUID
    event_type: str
    occurred_at: datetime
    source_node_id: UUID
    aggregate_type: str
    aggregate_public_id: UUID
    schema_version: int
    payload: Mapping[str, object]


def normalize_wire_event(envelope, contract):
    """Validate and normalize one inbound WireEventEnvelope against its
    resolved SyncEventContract, BEFORE any persistence or duplicate
    comparison. Raises WireEventValidationError for any defect; the
    caller (linuxlab.sync.ingestion.ingest_event()) maps that
    uniformly to a per-event REJECTED ACK."""
    if not isinstance(envelope.event_id, UUID):
        raise WireEventValidationError("event_id must be a UUID")
    if not isinstance(envelope.source_node_id, UUID):
        raise WireEventValidationError("source_node_id must be a UUID")
    if not isinstance(envelope.aggregate_public_id, UUID):
        raise WireEventValidationError("aggregate_public_id must be a UUID")
    if type(envelope.schema_version) is not int or envelope.schema_version < 1:
        raise WireEventValidationError("schema_version must be an integer >= 1")

    if not isinstance(envelope.event_type, str) or not envelope.event_type:
        raise WireEventValidationError("event_type must be a non-empty string")
    if len(envelope.event_type) > _MAX_EVENT_TYPE_LENGTH:
        raise WireEventValidationError(
            f"event_type exceeds maximum length of {_MAX_EVENT_TYPE_LENGTH}"
        )

    if not isinstance(envelope.aggregate_type, str) or not envelope.aggregate_type:
        raise WireEventValidationError("aggregate_type must be a non-empty string")
    if len(envelope.aggregate_type) > _MAX_AGGREGATE_TYPE_LENGTH:
        raise WireEventValidationError(
            f"aggregate_type exceeds maximum length of {_MAX_AGGREGATE_TYPE_LENGTH}"
        )
    if contract.expected_aggregate_type and envelope.aggregate_type != contract.expected_aggregate_type:
        raise WireEventValidationError(
            f"aggregate_type {envelope.aggregate_type!r} does not match the expected "
            f"{contract.expected_aggregate_type!r} for event_type {envelope.event_type!r}"
        )

    occurred_at = envelope.occurred_at
    if not isinstance(occurred_at, datetime):
        raise WireEventValidationError("occurred_at must be a datetime")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise WireEventValidationError("occurred_at must be timezone-aware, not naive")
    occurred_at_utc = occurred_at.astimezone(UTC)

    try:
        contract.validate_payload(envelope.payload)
    except ValueError as exc:
        raise WireEventValidationError(str(exc)) from exc

    canonical_aggregate_id = contract.payload_aggregate_public_id(envelope.payload)
    if canonical_aggregate_id is not None and canonical_aggregate_id != envelope.aggregate_public_id:
        raise WireEventValidationError(
            "aggregate_public_id does not match the payload's own canonical identity"
        )

    return NormalizedWireEvent(
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        occurred_at=occurred_at_utc,
        source_node_id=envelope.source_node_id,
        aggregate_type=envelope.aggregate_type,
        aggregate_public_id=envelope.aggregate_public_id,
        schema_version=envelope.schema_version,
        payload=envelope.payload,
    )


_ACK_STATUSES = frozenset({ACCEPTED, DUPLICATE, REJECTED})


def parse_ack_results(raw_results):
    """Build a BatchAck from an untrusted list of raw per-event result
    dicts (T14-D30 corrective, independent review finding #7; duplicate-
    detection ordering hardened per second-corrective finding #3): the
    conservative safety rules below apply BEFORE BatchAck is even
    constructed, so every transport implementation gets them for free
    rather than reimplementing its own ad hoc parser.

    Two phases, deliberately in this order:

    Phase 1 -- count every occurrence of each PARSEABLE event_id across
    ALL raw entries, BEFORE validating status/code. An entry whose
    event_id cannot be parsed as a UUID at all is excluded from this
    count (it cannot be attributed to any sent event). This is the
    second-corrective fix: the original implementation discarded a
    malformed entry (bad status, or a `rejected` entry with no code)
    before it was ever counted, which meant a malformed duplicate could
    silently mask that its event_id was ALSO claimed by a different,
    otherwise-valid entry -- letting that other entry's verdict
    (`accepted`, say) slip through as if it were unambiguous.

    Phase 2 -- only an event_id that occurred EXACTLY ONCE in phase 1
    proceeds to status/code validation and, if it passes, becomes one
    actionable EventAck. An event_id that occurred more than once is
    ambiguous regardless of what its entries say (even if they are
    byte-for-byte identical) and never produces an actionable EventAck
    for that event -- it stays unconfirmed/retryable, never inferred
    delivered or dead-lettered.
    """
    items_by_event_id: dict[UUID, list[dict]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        try:
            event_id = UUID(str(item.get("event_id")))
        except (ValueError, TypeError, AttributeError):
            continue
        items_by_event_id.setdefault(event_id, []).append(item)

    acks = []
    for event_id, items in items_by_event_id.items():
        if len(items) != 1:
            continue
        item = items[0]
        status_value = item.get("status")
        if status_value not in _ACK_STATUSES:
            continue
        code = str(item.get("code") or "")
        if status_value == REJECTED and not code:
            continue
        acks.append(
            EventAck(
                event_id=event_id,
                status=status_value,
                code=code,
                reason=str(item.get("reason") or ""),
            )
        )

    return BatchAck(results=tuple(acks))
