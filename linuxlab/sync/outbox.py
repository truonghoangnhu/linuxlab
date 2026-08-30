"""Task 14 Outbox claim/lease/retry/delivery state machine
(T14-D13, T14-D14, T14-D15, T14-D16, T14-D17).

claim_outbox_batch() is the ONLY reader that transitions PENDING (or
lease-expired IN_FLIGHT) rows to IN_FLIGHT, under
select_for_update(skip_locked=True), inside one short transaction. It
performs no network I/O and no transport call -- callers (Task 10's
flush_outbox()) claim first, commit, THEN call a SyncTransport outside
any open transaction.

mark_delivered()/mark_dead_letter()/release_transient()/
release_auth_failure() are the only controlled writers of Outbox
delivery metadata (state, attempts, next_attempt_at, claimed_at,
lease_until, delivered_at, rejection_code, rejection_reason). None of
them can touch the immutable wire-envelope fields (event_id through
payload) -- this module never bypasses the ORM guard directly; every
write goes through linuxlab.sync.models' own model-fixed allow-list
(_write_immutable_row_fields()/_bulk_write_immutable_rows()), the same
single trust boundary linuxlab.sync.authentication and
linuxlab.sync.processing use (corrective, independent review finding
#2 -- the allow-list is fixed inside that boundary, never passed in by
this or any other caller).
"""

import random
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .contracts import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    AuthTransportError,
    ProtocolTransportError,
    TransientTransportError,
    WireEventEnvelope,
)
from .models import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_DELIVERED,
    OUTBOX_IN_FLIGHT,
    OUTBOX_PENDING,
    OutboxMessage,
    _bulk_write_immutable_rows,
    _write_immutable_row_fields,
)

# Task 5 owns sane self-contained defaults; Task 6/10 may later thread
# LINUXLAB_SYNC_BACKOFF_BASE_SECONDS/CAP_SECONDS settings through
# release_transient()'s caller without changing this module's contract.
_DEFAULT_BACKOFF_BASE_SECONDS = 5.0
_DEFAULT_BACKOFF_CAP_SECONDS = 3600.0


def _bulk_write_outbox_fields(event_ids, **fields):
    """Thin event_id-keyed convenience wrapper over
    linuxlab.sync.models._bulk_write_immutable_rows() -- the public
    event_id is the natural key every caller in this module already
    has, rather than a local pk. The mutable-field allow-list itself is
    resolved entirely inside that shared helper, not here. Affecting
    zero rows for an unknown/no-longer-matching event_id is a normal,
    safe no-op (the caller's next claim cycle simply will not see that
    row again), not an error."""
    event_ids = list(event_ids)
    if not event_ids:
        return 0
    return _bulk_write_immutable_rows(
        OutboxMessage.objects.filter(event_id__in=event_ids), **fields
    )


def envelope_for(outbox_message):
    """Rebuild the exact WireEventEnvelope originally captured at enqueue
    time from one OutboxMessage row -- never a reserialization of
    current domain state (T14-D11)."""
    return WireEventEnvelope(
        event_id=outbox_message.event_id,
        event_type=outbox_message.event_type,
        occurred_at=outbox_message.occurred_at,
        source_node_id=outbox_message.source_node_id,
        aggregate_type=outbox_message.aggregate_type,
        aggregate_public_id=outbox_message.aggregate_public_id,
        schema_version=outbox_message.schema_version,
        payload=outbox_message.payload,
    )


def claim_outbox_batch(*, max_events, max_bytes, lease_seconds, now=None):
    """Claim a bounded batch of due OutboxMessage rows: due PENDING rows,
    plus IN_FLIGHT rows whose lease has expired (a prior worker crashed
    or was killed mid-delivery). Selection stops at max_events, or
    before a row's canonical envelope bytes would push the running
    total over max_bytes -- except the batch always includes at least
    one row when any candidate exists, so a single oversized event can
    never starve itself forever; a second oversized row simply remains
    PENDING and eligible for a later call, never silently marked
    delivered or dead-lettered.

    Runs entirely inside one short transaction with
    select_for_update(skip_locked=True); performs no network I/O.
    """
    from .contracts import canonical_event_bytes

    now = now or timezone.now()
    lease_until = now + timedelta(seconds=lease_seconds)

    with transaction.atomic():
        # Bounded to max_events: SELECT FOR UPDATE locks every row it
        # returns, not just the ones ultimately selected below, so an
        # unbounded fetch here would lock rows a concurrent claimer
        # could otherwise take (starving it via skip_locked). Byte-budget
        # trimming only ever removes from the tail of this same window,
        # so it never needs to see past the max_events'th due candidate.
        candidates = list(
            OutboxMessage.objects.select_for_update(skip_locked=True)
            .filter(
                Q(state=OUTBOX_PENDING, next_attempt_at__lte=now)
                | Q(state=OUTBOX_IN_FLIGHT, lease_until__lte=now)
            )
            .order_by("next_attempt_at", "created_at", "pk")[:max_events]
        )

        selected = []
        running_bytes = 0
        for row in candidates:
            if len(selected) >= max_events:
                break
            row_bytes = len(canonical_event_bytes(envelope_for(row)))
            if selected and running_bytes + row_bytes > max_bytes:
                break
            selected.append(row)
            running_bytes += row_bytes

        claimed_pks = [row.pk for row in selected]
        if claimed_pks:
            _bulk_write_immutable_rows(
                OutboxMessage.objects.filter(pk__in=claimed_pks),
                state=OUTBOX_IN_FLIGHT,
                claimed_at=now,
                lease_until=lease_until,
            )

    for row in selected:
        row.state = OUTBOX_IN_FLIGHT
        row.claimed_at = now
        row.lease_until = lease_until

    return tuple(selected)


def mark_delivered(event_ids, now=None):
    """Transition confirmed accepted/duplicate events to DELIVERED
    (T14-D08, T14-D17). Never called for an unconfirmed event_id."""
    now = now or timezone.now()
    _bulk_write_outbox_fields(event_ids, state=OUTBOX_DELIVERED, delivered_at=now)


def mark_dead_letter(event_id, code, reason, now=None):
    """Permanently reject exactly one event (T14-D17). A poison event's
    dead-letter never affects any other event in the same batch."""
    del now  # no timestamp field beyond rejection_code/reason is required
    _bulk_write_outbox_fields(
        [event_id], state=OUTBOX_DEAD_LETTER, rejection_code=code, rejection_reason=reason
    )


def release_transient(event_ids, error_kind, now=None):
    """Return claimed events to PENDING after a transient delivery
    failure (timeout, 5xx, missing ACK): attempts increments, and
    next_attempt_at moves out by an exponential-backoff-with-jitter
    delay. Never dead-letters, at any attempt count (T14-D14, T14-D15).
    """
    del error_kind  # reserved for future differentiated backoff policy
    now = now or timezone.now()
    rows = OutboxMessage.objects.filter(event_id__in=list(event_ids))
    for row in rows:
        new_attempts = row.attempts + 1
        delay = compute_retry_delay(
            new_attempts,
            base=_DEFAULT_BACKOFF_BASE_SECONDS,
            cap=_DEFAULT_BACKOFF_CAP_SECONDS,
            random_source=random.random,
        )
        _write_immutable_row_fields(
            row,
            state=OUTBOX_PENDING,
            attempts=new_attempts,
            next_attempt_at=now + timedelta(seconds=delay),
            claimed_at=None,
            lease_until=None,
        )


def release_auth_failure(event_ids, now=None):
    """Return claimed events to PENDING after a 401/403 channel fault --
    a fault in the Node's credential/channel, not in any individual
    event's content, so attempts is left untouched and the event is
    immediately retryable once the channel recovers (T14-D17)."""
    now = now or timezone.now()
    _bulk_write_outbox_fields(
        event_ids,
        state=OUTBOX_PENDING,
        next_attempt_at=now,
        claimed_at=None,
        lease_until=None,
    )


def compute_retry_delay(attempts, *, base, cap, random_source):
    """Full-jitter exponential backoff: the exponential ceiling
    (base * 2**attempts, capped at `cap`) grows without bound on
    attempts and never raises/terminates -- there is no finite-attempt
    condition under which this stops being retryable. `random_source`
    is an injectable no-arg callable returning a value in [0, 1), so
    tests can assert exact, deterministic delays."""
    # 2**64 already dwarfs any realistic cap for any positive base, so
    # clamping the exponent avoids int-to-float overflow at very large
    # attempt counts without changing the effective (capped) result.
    safe_attempts = min(attempts, 64)
    ceiling = min(cap, base * (2**safe_attempts))
    return ceiling * random_source()


@dataclass(frozen=True)
class FlushSummary:
    """The outcome of one flush_outbox() cycle -- owns no business
    mutation and no Central projection; purely a report of what
    happened to claimed Outbox rows."""

    claimed: int = 0
    delivered: int = 0
    dead_lettered: int = 0
    released_transient: int = 0
    released_auth_failure: int = 0
    auth_fault: bool = False


def flush_outbox(transport, *, max_events, max_bytes, lease_seconds, now=None):
    """Claim a bounded batch, send it through `transport` OUTSIDE any
    open DB transaction, then update delivery metadata only for
    confirmed event IDs (T14-D08, T14-D16, T14-D17). Never reserializes
    from current domain state -- every envelope sent is exactly what
    claim_outbox_batch() returned. Owns no business mutation and no
    Central projection.
    """
    claimed = claim_outbox_batch(
        max_events=max_events, max_bytes=max_bytes, lease_seconds=lease_seconds, now=now
    )
    if not claimed:
        return FlushSummary()

    event_ids = [row.event_id for row in claimed]
    envelopes = tuple(envelope_for(row) for row in claimed)

    try:
        batch_ack = transport.send_batch(envelopes)
    except AuthTransportError:
        release_auth_failure(event_ids, now=now)
        return FlushSummary(claimed=len(claimed), released_auth_failure=len(claimed), auth_fault=True)
    except (TransientTransportError, ProtocolTransportError) as exc:
        release_transient(event_ids, type(exc).__name__, now=now)
        return FlushSummary(claimed=len(claimed), released_transient=len(claimed))

    acks_by_event_id = {ack.event_id: ack for ack in batch_ack.results}

    delivered_ids = []
    dead_lettered = 0
    unconfirmed_ids = []
    for event_id in event_ids:
        ack = acks_by_event_id.get(event_id)
        if ack is None:
            unconfirmed_ids.append(event_id)
            continue
        if ack.status in (ACCEPTED, DUPLICATE):
            delivered_ids.append(event_id)
        elif ack.status == REJECTED:
            mark_dead_letter(event_id, ack.code, ack.reason, now=now)
            dead_lettered += 1
        else:
            unconfirmed_ids.append(event_id)

    if delivered_ids:
        mark_delivered(delivered_ids, now=now)
    if unconfirmed_ids:
        release_transient(unconfirmed_ids, "missing_or_unconfirmed_ack", now=now)

    return FlushSummary(
        claimed=len(claimed),
        delivered=len(delivered_ids),
        dead_lettered=dead_lettered,
        released_transient=len(unconfirmed_ids),
    )
