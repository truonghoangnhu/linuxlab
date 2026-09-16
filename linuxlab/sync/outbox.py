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
lease_until, claim_token, delivered_at, rejection_code,
rejection_reason). None of them can touch the immutable wire-envelope
fields (event_id through payload) -- this module never bypasses the
ORM guard directly; every write goes through linuxlab.sync.models' own
model-fixed allow-list (_write_immutable_row_fields()/
_bulk_write_immutable_rows()), the same single trust boundary
linuxlab.sync.authentication and linuxlab.sync.processing use
(corrective, independent review finding #2 -- the allow-list is fixed
inside that boundary, never passed in by this or any other caller).

Every one of those four writers also requires the caller to pass the
EXACT claimed OutboxMessage row(s) it received from claim_outbox_batch()
-- not a bare event_id -- and fences its write to state=IN_FLIGHT plus
that row's exact claim_token (corrective, independent review finding
#3). A worker whose lease already expired and was reclaimed by another
worker therefore affects zero rows when it later reports a result,
instead of resurrecting DELIVERED/DEAD_LETTER or clobbering the
reclaiming worker's own transition.
"""

import random
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .configuration import SyncConfigurationError, load_sync_configuration
from .contracts import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    AuthTransportError,
    ProtocolTransportError,
    RequestTooLargeError,
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
)

BackoffConfigurationError = SyncConfigurationError


def _validated_backoff_bounds():
    """Read current bounds through the canonical retry configuration authority."""
    policy = load_sync_configuration(purpose="retry").retry_policy
    return policy.base_delay, policy.max_delay


def _configured_backoff_base_seconds():
    """Task 15: LINUXLAB_SYNC_BACKOFF_BASE_SECONDS actually drives
    ordinary transient retry timing (closing the accepted Task 14
    Minor -- this used to be a hardcoded module constant the setting
    never reached)."""
    base, _cap = _validated_backoff_bounds()
    return base


def _configured_backoff_cap_seconds():
    _base, cap = _validated_backoff_bounds()
    return cap


def _configured_protocol_fault_backoff_seconds():
    """T14-D41 singleton-400 backoff. Checkpoint corrective (finding
    #5): the ordinary transient path's full-jitter delay is
    `ceiling * random_source()` with `ceiling = min(cap, base * 2**n)`,
    so its worst-case ceiling can itself reach exactly `cap` -- a
    bare-cap singleton delay could then tie with that worst-case ceiling
    instead of staying strictly longer. Doubling the cap here is a
    fixed, non-configurable multiplier (not a new operator-facing
    setting) that keeps this always strictly greater than the ordinary
    path's maximum possible ceiling, for any validated base/cap."""
    _base, cap = _validated_backoff_bounds()
    return cap * 2


def _write_fenced(row, **changes):
    """Update `row`'s OutboxMessage ONLY if it still owns the exact
    claim it was issued: state=IN_FLIGHT AND this exact claim_token
    (corrective, independent review finding #3). Returns the number of
    rows updated -- 0 is a legitimate, safe outcome (a stale worker
    fenced out by a reclaim), never an error."""
    return _bulk_write_immutable_rows(
        OutboxMessage.objects.filter(
            event_id=row.event_id, state=OUTBOX_IN_FLIGHT, claim_token=row.claim_token
        ),
        **changes,
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

    Byte-budget accounting uses serialize_sync_batch() -- the SAME
    function HttpSyncTransport uses to build the actual outgoing
    request body -- so the locally enforced count can never mismatch
    what Central actually receives on the wire (corrective, independent
    review finding #4).

    Runs entirely inside one short transaction with
    select_for_update(skip_locked=True); performs no network I/O.
    """
    from .contracts import serialize_sync_batch

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
        selected_envelopes = []
        for row in candidates:
            if len(selected) >= max_events:
                break
            row_envelope = envelope_for(row)
            candidate_bytes = len(serialize_sync_batch([*selected_envelopes, row_envelope]))
            if selected and candidate_bytes > max_bytes:
                break
            selected.append(row)
            selected_envelopes.append(row_envelope)

        # A fresh claim_token per row, minted on every claim AND every
        # reclaim, is what lets every later write prove it still owns
        # the row's current claim (T14 corrective, review finding #3).
        new_tokens = {}
        for row in selected:
            token = uuid.uuid4()
            new_tokens[row.pk] = token
            _bulk_write_immutable_rows(
                OutboxMessage.objects.filter(pk=row.pk),
                state=OUTBOX_IN_FLIGHT,
                claimed_at=now,
                lease_until=lease_until,
                claim_token=token,
            )

    for row in selected:
        row.state = OUTBOX_IN_FLIGHT
        row.claimed_at = now
        row.lease_until = lease_until
        row.claim_token = new_tokens[row.pk]

    return tuple(selected)


def mark_delivered(claimed_rows, now=None):
    """Transition confirmed accepted/duplicate events to DELIVERED
    (T14-D08, T14-D17). Never called for an unconfirmed event. Each
    `claimed_rows` entry must be the exact OutboxMessage claim_outbox_batch()
    returned; a stale/reclaimed row's write is fenced to zero rows."""
    now = now or timezone.now()
    for row in claimed_rows:
        _write_fenced(row, state=OUTBOX_DELIVERED, delivered_at=now)


def mark_dead_letter(claimed_row, code, reason, now=None):
    """Permanently reject exactly one event (T14-D17). A poison event's
    dead-letter never affects any other event in the same batch. Fenced
    the same way mark_delivered() is."""
    del now  # no timestamp field beyond rejection_code/reason is required
    _write_fenced(
        claimed_row, state=OUTBOX_DEAD_LETTER, rejection_code=code, rejection_reason=reason
    )


def release_transient(claimed_rows, error_kind, now=None):
    """Return claimed events to PENDING after a transient delivery
    failure (timeout, 5xx, missing ACK): attempts increments, and
    next_attempt_at moves out by an exponential-backoff-with-jitter
    delay. Never dead-letters, at any attempt count (T14-D14, T14-D15).
    Fenced the same way mark_delivered() is; the claim_token is cleared
    since the row no longer has an active claim.
    """
    del error_kind  # reserved for future differentiated backoff policy
    now = now or timezone.now()
    for row in claimed_rows:
        new_attempts = row.attempts + 1
        delay = compute_retry_delay(
            new_attempts,
            base=_configured_backoff_base_seconds(),
            cap=_configured_backoff_cap_seconds(),
            random_source=random.random,
        )
        _write_fenced(
            row,
            state=OUTBOX_PENDING,
            attempts=new_attempts,
            next_attempt_at=now + timedelta(seconds=delay),
            claimed_at=None,
            lease_until=None,
            claim_token=None,
        )


def release_auth_failure(claimed_rows, now=None):
    """Return claimed events to PENDING after a 401/403 channel fault --
    a fault in the Node's credential/channel, not in any individual
    event's content, so attempts is left untouched and the event is
    immediately retryable once the channel recovers (T14-D17). Fenced
    the same way mark_delivered() is."""
    now = now or timezone.now()
    for row in claimed_rows:
        _write_fenced(
            row,
            state=OUTBOX_PENDING,
            next_attempt_at=now,
            claimed_at=None,
            lease_until=None,
            claim_token=None,
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
    # T14-D40 corrective: a deterministic 400 (never 413 -- that has its
    # own split/singleton-dead-letter handling below) is flagged here so
    # a caller (the sync_events worker) can pause/degrade visibly instead
    # of silently retrying an identical malformed request forever.
    protocol_fault: bool = False


def _merge_flush_summaries(a, b):
    return FlushSummary(
        claimed=a.claimed + b.claimed,
        delivered=a.delivered + b.delivered,
        dead_lettered=a.dead_lettered + b.dead_lettered,
        released_transient=a.released_transient + b.released_transient,
        released_auth_failure=a.released_auth_failure + b.released_auth_failure,
        auth_fault=a.auth_fault or b.auth_fault,
        protocol_fault=a.protocol_fault or b.protocol_fault,
    )


def _dead_letter_event_too_large(row, now):
    mark_dead_letter(
        row,
        "EVENT_TOO_LARGE",
        "singleton event still rejected as too large by Central",
        now=now,
    )
    return FlushSummary(claimed=1, dead_lettered=1)


def _release_singleton_protocol_fault(row, now):
    # T14-D41: deliberately much longer than the ordinary base-driven
    # exponential backoff -- a 400 singleton has already been isolated
    # as the specific cause of a request-level fault this Task 14
    # implementation itself constructed, so it is not retried on the
    # ordinary short cadence, but it is also never permanently
    # unsendable the way a 413 singleton is (T14-D40), so it stays
    # PENDING rather than DEAD_LETTER. Deliberately tracks the
    # configured cap (Task 15) rather than a fixed number, but is read
    # independently of `base` -- it must never collapse to the ordinary
    # transient schedule regardless of how `base` is configured. See
    # _configured_protocol_fault_backoff_seconds() for why this is 2x
    # the cap, not the bare cap value (checkpoint corrective finding #5).
    now = now or timezone.now()
    long_backoff_seconds = _configured_protocol_fault_backoff_seconds()
    _write_fenced(
        row,
        state=OUTBOX_PENDING,
        next_attempt_at=now + timedelta(seconds=long_backoff_seconds),
        claimed_at=None,
        lease_until=None,
        claim_token=None,
    )
    return FlushSummary(claimed=1, released_transient=1, protocol_fault=True)


def _isolate_and_retry(transport, rows, now, on_singleton_terminal):
    """Shared T14-D40/T14-D41 isolation: bisect `rows` and retry each
    half through _send_claimed_rows() independently until a half
    shrinks to one event, at which point `on_singleton_terminal(row, now)`
    decides that event's terminal outcome (permanent DEAD_LETTER for a
    413 singleton; retryable PENDING with long backoff for a 400
    singleton) and returns a one-event FlushSummary. Bounded by simple
    halving -- O(log n) deep for any realistic batch, never unbounded.
    """
    if len(rows) == 1:
        return on_singleton_terminal(rows[0], now)
    midpoint = len(rows) // 2
    first_half = _send_claimed_rows(transport, rows[:midpoint], now=now)
    second_half = _send_claimed_rows(transport, rows[midpoint:], now=now)
    return _merge_flush_summaries(first_half, second_half)


def _send_claimed_rows(transport, rows, now=None):
    """Send exactly `rows` (already-claimed OutboxMessage instances)
    through `transport`, OUTSIDE any open DB transaction, then update
    delivery metadata only for confirmed events.

    Handles T14-D40 (413) recovery and T14-D41 (400 isolation) with the
    same bisection mechanism, but distinct terminal semantics: a
    singleton that still receives 413 is permanently unsendable under
    the current transport contract and dead-letters with code
    EVENT_TOO_LARGE; a singleton that still receives 400 is a
    request-shape fault, not an inherent property of the event, so it
    stays retryable (PENDING) with a long protocol-specific backoff and
    an operator-visible protocol_fault flag -- never DEAD_LETTER, never
    EVENT_TOO_LARGE. Neither case ever reconstructs and resends the
    original, larger blocking batch unchanged; unaffected subsets/events
    always complete independently.
    """
    if not rows:
        return FlushSummary()

    envelopes = tuple(envelope_for(row) for row in rows)

    try:
        batch_ack = transport.send_batch(envelopes)
    except RequestTooLargeError:
        return _isolate_and_retry(transport, rows, now, _dead_letter_event_too_large)
    except AuthTransportError:
        release_auth_failure(rows, now=now)
        return FlushSummary(claimed=len(rows), released_auth_failure=len(rows), auth_fault=True)
    except ProtocolTransportError:
        return _isolate_and_retry(transport, rows, now, _release_singleton_protocol_fault)
    except TransientTransportError:
        release_transient(rows, "transient", now=now)
        return FlushSummary(claimed=len(rows), released_transient=len(rows))

    # Never trust that batch_ack.results is already deduplicated --
    # BatchAck can be constructed directly by any transport (or a test),
    # so this consumption is ambiguity-safe on its own terms rather than
    # relying solely on parse_ack_results() upstream (corrective,
    # independent review finding #7). An event_id that appears more than
    # once here is ambiguous and stays unconfirmed, exactly like a
    # REJECTED result with no rejection code.
    acks_by_event_id = {}
    ambiguous_event_ids = set()
    for ack in batch_ack.results:
        if ack.event_id in acks_by_event_id:
            ambiguous_event_ids.add(ack.event_id)
            continue
        acks_by_event_id[ack.event_id] = ack

    delivered_rows = []
    dead_lettered = 0
    unconfirmed_rows = []
    for row in rows:
        if row.event_id in ambiguous_event_ids:
            unconfirmed_rows.append(row)
            continue
        ack = acks_by_event_id.get(row.event_id)
        if ack is None:
            unconfirmed_rows.append(row)
            continue
        if ack.status in (ACCEPTED, DUPLICATE):
            delivered_rows.append(row)
        elif ack.status == REJECTED:
            if not ack.code:
                unconfirmed_rows.append(row)
                continue
            mark_dead_letter(row, ack.code, ack.reason, now=now)
            dead_lettered += 1
        else:
            unconfirmed_rows.append(row)

    if delivered_rows:
        mark_delivered(delivered_rows, now=now)
    if unconfirmed_rows:
        release_transient(unconfirmed_rows, "missing_or_unconfirmed_ack", now=now)

    return FlushSummary(
        claimed=len(rows),
        delivered=len(delivered_rows),
        dead_lettered=dead_lettered,
        released_transient=len(unconfirmed_rows),
    )


def flush_outbox(transport, *, max_events, max_bytes, lease_seconds, now=None):
    """Claim a bounded batch, send it through `transport` OUTSIDE any
    open DB transaction, then update delivery metadata only for
    confirmed events (T14-D08, T14-D16, T14-D17, T14-D40). Never
    reserializes from current domain state -- every envelope sent is
    exactly what claim_outbox_batch() returned. Owns no business
    mutation and no Central projection.
    """
    claimed = claim_outbox_batch(
        max_events=max_events, max_bytes=max_bytes, lease_seconds=lease_seconds, now=now
    )
    return _send_claimed_rows(transport, list(claimed), now=now)
