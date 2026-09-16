"""Task 14 explicit transactional enqueue and the `activity.completed`
vertical slice (T14-D06, T14-D10, T14-D11, T14-D20, T14-D21, T14-D32,
T14-D38).

Business/application services call enqueue_sync_event() explicitly,
inside the SAME transaction that records the domain mutation and the
DomainEvent -- never a Django post_save signal, never a background
scanner (T14-D32). This module also defines and registers the one real
Task 14 reference event: `activity.completed` -- the only production
DomainEvent type actually published as of Task 13 HEAD, from
linuxlab.learning.services.claim_activity_completion() and
submit_attempt() (both immediately after Result.objects.create(), in the
same transaction). No other event type is registered/syncable.
"""

import logging
import uuid

from .configuration import DeferredSyncSettingError, parse_sync_setting
from .contracts import SyncEventContract, WireEventEnvelope
from .models import OutboxMessage, _insert_immutable_row
from .registry import get_current_sync_event_contract, register_sync_event_contract

logger = logging.getLogger(__name__)


class SyncConfigurationError(Exception):
    """Raised when sync is enabled but the Node identity required to
    enqueue a syncable event is missing/invalid -- a fail-loud
    configuration error, never a silently dropped event (T14-D38)."""


def enqueue_sync_event(domain_event, *, node_id, sync_enabled):
    """Explicitly enqueue one immutable OutboxMessage snapshot for
    `domain_event`, inside the caller's own transaction (T14-D10). The
    caller must invoke this itself, in the same transaction.atomic()
    block that recorded the domain mutation and domain_event.

    Returns None if optional sync admission is malformed (with a safe
    operator warning), if sync is disabled, or if
    domain_event.event_type has no registered contract (LOCAL-ONLY by
    default, T14-D20). Raises SyncConfigurationError if sync is enabled
    but node_id is missing/falsy -- a configuration error must never
    silently drop a syncable event (T14-D38). A temporarily invalid/
    revoked secret is a transport-layer concern (see authentication.py/
    transports.py), never a reason to refuse local enqueue.
    """
    admission = parse_sync_setting("LINUXLAB_SYNC_ENABLED", sync_enabled)
    if isinstance(admission, DeferredSyncSettingError):
        # Isolate only optional admission. Keep the original setting invalid
        # for strict consumers; do not catch serialization or database errors.
        logger.warning("SYNC_ENQUEUE_SKIPPED_INVALID_ENABLED: optional sync export skipped.")
        return None
    if not admission:
        return None

    contract = get_current_sync_event_contract(domain_event.event_type)
    if contract is None:
        return None

    if not node_id:
        raise SyncConfigurationError(
            "sync is enabled but no Node identity is configured; cannot "
            f"enqueue syncable event {domain_event.event_type!r}"
        )

    aggregate_type, aggregate_public_id, payload = contract.serialize_domain_event(domain_event)

    envelope = WireEventEnvelope(
        event_id=domain_event.event_id,
        event_type=contract.event_type,
        occurred_at=domain_event.created_at,
        source_node_id=node_id,
        aggregate_type=aggregate_type,
        aggregate_public_id=aggregate_public_id,
        schema_version=contract.schema_version,
        payload=payload,
    )

    return _insert_immutable_row(
        OutboxMessage,
        event_id=envelope.event_id,
        domain_event=domain_event,
        event_type=envelope.event_type,
        schema_version=envelope.schema_version,
        source_node_id=envelope.source_node_id,
        occurred_at=envelope.occurred_at,
        aggregate_type=envelope.aggregate_type,
        aggregate_public_id=envelope.aggregate_public_id,
        payload=dict(envelope.payload),
    )


# --- the one real Task 14 vertical slice: activity.completed ---------------

_ACTIVITY_COMPLETED_WIRE_FIELDS = (
    "attempt_public_id",
    "learner_public_id",
    "activity_definition_public_id",
    "activity_version",
    "score",
    "max_score",
)
_ACTIVITY_COMPLETED_WIRE_FIELD_SET = frozenset(_ACTIVITY_COMPLETED_WIRE_FIELDS)


def _serialize_activity_completed(domain_event):
    """Extract exactly the minimal learner-safe subset already present at
    publish_event() time -- never a raw DomainEvent.payload dump. The
    guest-claim call site's payload additionally carries a "source" key
    the submit_attempt() call site does not; that (and anything else not
    in the wire field set) is deliberately dropped here, not forwarded."""
    payload = domain_event.payload
    wire_payload = {key: payload[key] for key in _ACTIVITY_COMPLETED_WIRE_FIELDS}
    aggregate_type = "attempt"
    aggregate_public_id = domain_event.aggregate_id
    return aggregate_type, aggregate_public_id, wire_payload


def _validate_activity_completed_payload(payload):
    """Central-side validation of an incoming wire payload for this
    contract (used by ingestion.py before anything is persisted)."""
    if not isinstance(payload, dict):
        raise ValueError("activity.completed payload must be an object")  # noqa: TRY004

    actual_keys = set(payload.keys())
    if actual_keys != _ACTIVITY_COMPLETED_WIRE_FIELD_SET:
        missing = sorted(_ACTIVITY_COMPLETED_WIRE_FIELD_SET - actual_keys)
        extra = sorted(actual_keys - _ACTIVITY_COMPLETED_WIRE_FIELD_SET)
        raise ValueError(
            f"activity.completed payload fields mismatch (missing={missing}, extra={extra})"
        )

    if type(payload["activity_version"]) is not int or payload["activity_version"] < 1:
        raise ValueError("activity.completed payload activity_version must be an integer >= 1")
    if type(payload["score"]) is not int or payload["score"] < 0:
        raise ValueError("activity.completed payload score must be a non-negative integer")
    if type(payload["max_score"]) is not int or payload["max_score"] < 0:
        raise ValueError("activity.completed payload max_score must be a non-negative integer")
    if payload["score"] > payload["max_score"]:
        raise ValueError("activity.completed payload score cannot exceed max_score")
    for key in ("attempt_public_id", "learner_public_id", "activity_definition_public_id"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise ValueError(f"activity.completed payload {key} must be a non-empty string")
        try:
            uuid.UUID(payload[key])
        except ValueError as exc:
            raise ValueError(f"activity.completed payload {key} must be a valid UUID") from exc


def _activity_completed_payload_aggregate_public_id(payload):
    """The payload's own canonical attempt identity -- normalize_wire_event()
    rejects any envelope whose top-level aggregate_public_id doesn't
    match this (corrective, independent review finding #5)."""
    return uuid.UUID(payload["attempt_public_id"])


ACTIVITY_COMPLETED_V1 = SyncEventContract(
    event_type="activity.completed",
    schema_version=1,
    serialize_domain_event=_serialize_activity_completed,
    validate_payload=_validate_activity_completed_payload,
    expected_aggregate_type="attempt",
    payload_aggregate_public_id=_activity_completed_payload_aggregate_public_id,
)


def _register_default_contracts():
    """Idempotent registration: importing this module more than once
    (e.g. once via AppConfig.ready(), once via a direct test import) must
    not raise "already registered"."""
    if get_current_sync_event_contract(ACTIVITY_COMPLETED_V1.event_type) is None:
        register_sync_event_contract(ACTIVITY_COMPLETED_V1)


_register_default_contracts()
