"""Task 14 wire contracts (T14-D06, T14-D20, T14-D22): the stable
normalized envelope crossing Node <-> Central, deliberately separate from
DomainEvent's own persistence shape and from OutboxMessage/InboxMessage's
storage shape. Nothing here is Django-aware.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

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
    or malformed result is always treated as unconfirmed."""

    results: tuple[EventAck, ...] = ()

    def status_for(self, event_id):
        for result in self.results:
            if result.event_id == event_id:
                return result.status
        return None


class SyncTransport(Protocol):
    """T14-D01, T14-D02, T14-D29, T14-D39: the abstraction both
    HttpSyncTransport (production) and FileSyncTransport (dev/test/
    reference only) implement, over the identical WireEventEnvelope."""

    def send_batch(self, envelopes: tuple[WireEventEnvelope, ...]) -> BatchAck: ...


class TransportError(Exception):
    """Base for whole-batch (not per-event) transport failures raised by
    send_batch() -- always classify into exactly one of the subclasses
    below rather than raising this directly (T14-D29)."""


class TransientTransportError(TransportError):
    """Timeout, DNS/connect failure, 5xx, or a malformed/unparseable
    response -- always safely retryable; never a reason to dead-letter
    any event (T14-D14)."""


class AuthTransportError(TransportError):
    """401/403 -- a channel/credential fault, never an individual event's
    fault; callers must release the whole claimed batch back to PENDING
    without dead-lettering and without incrementing per-event attempts
    (T14-D17)."""


class ProtocolTransportError(TransportError):
    """400/413 -- the outgoing request itself was malformed/oversized;
    a Task 14 implementation-side batching fault, not any one event's
    fault, so it is handled the same as a transient failure by the
    flush orchestrator rather than dead-lettering individual events."""


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
    """

    event_type: str
    schema_version: int
    serialize_domain_event: Callable[[object], tuple[str, UUID, Mapping[str, object]]]
    validate_payload: Callable[[Mapping[str, object]], None] = field(
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
    """Deterministic canonical serialization: sorted keys, no delimiter
    ambiguity. Used both for immutable-envelope comparison (exact
    duplicate vs idempotency conflict, T14-D27) and for batch byte-size
    budgeting (T14-D16)."""
    return json.dumps(
        canonical_event_dict(envelope),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
