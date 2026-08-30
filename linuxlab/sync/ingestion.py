"""Task 14 durable Central Inbox ingestion (T14-D12, T14-D26, T14-D27).

ingest_event() is the sole entry point that turns one authenticated
Node's WireEventEnvelope into a durable InboxMessage row. Every event
gets its own short transaction/savepoint boundary, so one poison event
in a batch never rolls back a neighboring good event (that batch-level
independence is what Task 8's API loop relies on). ACK is only ever
returned AFTER the durable insert (or a confirmed exact duplicate) --
never before.

The envelope is fully validated and UTC-normalized (normalize_wire_event())
BEFORE any persistence or duplicate comparison is attempted -- this is
what lets a poison event map to a clean per-event REJECTED ACK instead
of an uncaught database exception, and what makes exact-duplicate
comparison immune to two replays of the same instant differing only in
UTC offset representation (corrective, independent review findings #5
and #6).
"""

from django.db import IntegrityError, transaction

from .contracts import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    EventAck,
    WireEventEnvelope,
    WireEventValidationError,
    canonical_event_bytes,
    normalize_wire_event,
)
from .models import InboxMessage, _insert_immutable_row
from .registry import get_current_sync_event_contract, get_sync_event_contract


def _envelope_for_existing(existing):
    return WireEventEnvelope(
        event_id=existing.event_id,
        event_type=existing.event_type,
        occurred_at=existing.occurred_at,
        source_node_id=existing.source_node.node_id,
        aggregate_type=existing.aggregate_type,
        aggregate_public_id=existing.aggregate_public_id,
        schema_version=existing.schema_version,
        payload=existing.payload,
    )


def _envelope_for_normalized(normalized):
    return WireEventEnvelope(
        event_id=normalized.event_id,
        event_type=normalized.event_type,
        occurred_at=normalized.occurred_at,
        source_node_id=normalized.source_node_id,
        aggregate_type=normalized.aggregate_type,
        aggregate_public_id=normalized.aggregate_public_id,
        schema_version=normalized.schema_version,
        payload=normalized.payload,
    )


def ingest_event(authenticated_node, envelope):
    """Ingest one WireEventEnvelope on behalf of `authenticated_node`
    (a NodeRegistration already verified by
    linuxlab.sync.authentication.authenticate_node()). Returns an
    EventAck; never raises for an ordinary invalid/duplicate/conflicting
    event -- only a genuine unexpected infrastructure error propagates
    (corrective, independent review finding #5: this must never be
    disguised as a per-event payload rejection)."""
    if envelope.source_node_id != authenticated_node.node_id:
        return EventAck(
            event_id=envelope.event_id,
            status=REJECTED,
            code="SOURCE_MISMATCH",
            reason="envelope source_node_id does not match the authenticated Node",
        )

    contract = get_sync_event_contract(envelope.event_type, envelope.schema_version)
    if contract is None:
        if get_current_sync_event_contract(envelope.event_type) is None:
            return EventAck(
                event_id=envelope.event_id,
                status=REJECTED,
                code="UNSUPPORTED_EVENT",
                reason=f"event_type {envelope.event_type!r} is not syncable",
            )
        return EventAck(
            event_id=envelope.event_id,
            status=REJECTED,
            code="UNSUPPORTED_SCHEMA",
            reason=(
                f"event_type {envelope.event_type!r} has no registered contract for "
                f"schema_version {envelope.schema_version!r}"
            ),
        )

    try:
        normalized = normalize_wire_event(envelope, contract)
    except WireEventValidationError as exc:
        return EventAck(
            event_id=envelope.event_id, status=REJECTED, code="INVALID_PAYLOAD", reason=str(exc)
        )

    try:
        with transaction.atomic():
            _insert_immutable_row(
                InboxMessage,
                event_id=normalized.event_id,
                source_node=authenticated_node,
                event_type=normalized.event_type,
                schema_version=normalized.schema_version,
                occurred_at=normalized.occurred_at,
                aggregate_type=normalized.aggregate_type,
                aggregate_public_id=normalized.aggregate_public_id,
                payload=dict(normalized.payload),
            )
    except IntegrityError as integrity_exc:
        # Only classify duplicate/conflict after confirming a row with
        # this exact event_id actually exists -- an IntegrityError from
        # any OTHER cause (a genuine infrastructure/schema fault) must
        # re-raise, never be silently mistaken for an idempotency replay
        # (corrective, independent review finding #5).
        try:
            existing = InboxMessage.objects.select_related("source_node").get(
                event_id=normalized.event_id
            )
        except InboxMessage.DoesNotExist:
            raise integrity_exc from None

        if canonical_event_bytes(_envelope_for_existing(existing)) == canonical_event_bytes(
            _envelope_for_normalized(normalized)
        ):
            return EventAck(event_id=normalized.event_id, status=DUPLICATE)
        return EventAck(
            event_id=normalized.event_id,
            status=REJECTED,
            code="IDEMPOTENCY_CONFLICT",
            reason="an event with this event_id already exists with different content",
        )

    return EventAck(event_id=normalized.event_id, status=ACCEPTED)
