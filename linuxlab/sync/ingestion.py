"""Task 14 durable Central Inbox ingestion (T14-D12, T14-D26, T14-D27).

ingest_event() is the sole entry point that turns one authenticated
Node's WireEventEnvelope into a durable InboxMessage row. Every event
gets its own short transaction/savepoint boundary, so one poison event
in a batch never rolls back a neighboring good event (that batch-level
independence is what Task 8's API loop relies on). ACK is only ever
returned AFTER the durable insert (or a confirmed exact duplicate) --
never before.
"""

from django.db import IntegrityError, transaction

from .contracts import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    EventAck,
    WireEventEnvelope,
    canonical_event_bytes,
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


def ingest_event(authenticated_node, envelope):
    """Ingest one WireEventEnvelope on behalf of `authenticated_node`
    (a NodeRegistration already verified by
    linuxlab.sync.authentication.authenticate_node()). Returns an
    EventAck; never raises for an ordinary invalid/duplicate/conflicting
    event -- only a genuine unexpected DB error propagates."""
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
        contract.validate_payload(envelope.payload)
    except ValueError as exc:
        return EventAck(
            event_id=envelope.event_id, status=REJECTED, code="INVALID_PAYLOAD", reason=str(exc)
        )

    try:
        with transaction.atomic():
            _insert_immutable_row(
                InboxMessage,
                event_id=envelope.event_id,
                source_node=authenticated_node,
                event_type=envelope.event_type,
                schema_version=envelope.schema_version,
                occurred_at=envelope.occurred_at,
                aggregate_type=envelope.aggregate_type,
                aggregate_public_id=envelope.aggregate_public_id,
                payload=dict(envelope.payload),
            )
    except IntegrityError:
        existing = InboxMessage.objects.select_related("source_node").get(
            event_id=envelope.event_id
        )
        if canonical_event_bytes(_envelope_for_existing(existing)) == canonical_event_bytes(
            envelope
        ):
            return EventAck(event_id=envelope.event_id, status=DUPLICATE)
        return EventAck(
            event_id=envelope.event_id,
            status=REJECTED,
            code="IDEMPOTENCY_CONFLICT",
            reason="an event with this event_id already exists with different content",
        )

    return EventAck(event_id=envelope.event_id, status=ACCEPTED)
