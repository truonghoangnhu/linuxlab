"""Task 14 Outbox claim/lease/retry/delivery state machine (T14-D13,
T14-D14, T14-D15, T14-D16, T14-D17); Corrective B Task 8 makes
OutboxMessage's persisted retry-cycle fields (Task 7) the sole,
bounded, fenced retry authority (approved spec §§13-15).

claim_outbox_batch() is the ONLY reader that transitions PENDING (or
lease-expired IN_FLIGHT) rows to IN_FLIGHT, under
select_for_update(skip_locked=True), inside one short transaction. It
also enforces the retry budget as an eligibility rule: a due/reclaimable
candidate whose budget is already exhausted is terminalized to
dead_letter in that SAME short transaction -- never claimed, never sent,
never counted as a failed attempt. Only budget-eligible rows are ever
returned for a caller (flush_outbox()) to send outside any open
transaction.

record_delivery_failure() is the single authoritative, fenced writer for
every failed-send outcome: it re-resolves the exact claimed row (state,
claim_token, AND captured attempts) under select_for_update() inside a
short transaction, increments attempts exactly once, sets
first_failure_at only once, records safe failure evidence, and decides
dead_letter vs. pending-with-persisted-due-time from
failure.category and the SAME bounded budget. mark_delivered(),
mark_dead_letter(), release_transient(), and release_auth_failure()
remain the public compatibility surface but are now thin wrappers that
all funnel through this one authority -- no second counter, no second
policy, no second state machine. None of them can touch the immutable
wire-envelope fields (event_id through payload) -- this module never
bypasses the ORM guard directly; every write goes through
linuxlab.sync.models' own model-fixed allow-list
(_write_immutable_row_fields()/_bulk_write_immutable_rows()).
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
    DeliveryFailure,
    ProtocolTransportError,
    RequestTooLargeError,
    SyncOutcome,
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

BackoffConfigurationError = SyncConfigurationError

# Safe, fixed, controlled human-readable reasons -- never the remote
# transport's own exception/response text (Corrective B Task 8 §9/§27).
_SAFE_FAILURE_REASONS = {
    SyncOutcome.PERMANENT_REJECTION: "rejected by Central as a permanent failure",
    SyncOutcome.AUTH_OR_REGISTRATION_FAILURE: "Node credential rejected by Central",
    SyncOutcome.TRANSIENT_FAILURE: "transient delivery failure",
    SyncOutcome.RATE_LIMITED: "rate limited by Central",
}

_TERMINAL_CATEGORIES = frozenset(
    {SyncOutcome.PERMANENT_REJECTION, SyncOutcome.AUTH_OR_REGISTRATION_FAILURE}
)


def _current_retry_policy():
    """Read the current typed retry policy through the canonical Task 3
    configuration authority -- never parsed independently here."""
    return load_sync_configuration(purpose="retry").retry_policy


def _validated_backoff_bounds():
    """Read current bounds through the canonical retry configuration authority."""
    policy = _current_retry_policy()
    return policy.base_delay, policy.max_delay


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


def compute_retry_delay(attempts, *, base, cap, random_source):
    """Full-jitter exponential backoff (approved spec §14):

        n = max(attempts - 1, 0)
        cap_n = min(cap, base * 2**n)
        delay = uniform duration in [0, cap_n]

    so the FIRST failure's maximum delay is `base`, not `2*base`.
    `random_source` is an injectable no-arg callable returning a value
    in [0, 1] (production default is `random.random`, resolved at CALL
    time by callers -- never bound as a static default here, so test
    monkeypatching of `random.random` always takes effect).

    Bounded doubling, never a raw `base * 2**n`: for astronomically
    large `attempts` (or an extremely small but valid `base`), `n`
    itself may be enormous, but the number of DOUBLINGS actually needed
    to reach `cap` is always small and finite regardless of how large
    `n` is -- so this loop stops as soon as the ceiling reaches `cap`,
    never computing `2**n` directly and never truncating at an
    arbitrary fixed exponent (which would silently under-delay for a
    tiny base needing many more than 64 doublings to reach a large cap).
    """
    n = max(attempts - 1, 0)
    ceiling = base
    for _ in range(n):
        if ceiling >= cap:
            ceiling = cap
            break
        ceiling *= 2
    ceiling = min(ceiling, cap)
    return ceiling * random_source()


def retry_budget_remaining(attempts, first_failure_at, *, policy, now):
    """True only when BOTH the attempt budget and the age budget remain
    (approved spec §14): `attempts < policy.max_attempts` AND
    `now - first_failure_at < policy.max_retry_age`. Equality at EITHER
    boundary exhausts the budget.

    `attempts == 0` and `first_failure_at is None` together mean the
    first, untouched cycle -- always eligible. `attempts > 0` with
    `first_failure_at is None` is an invalid persisted state (it should
    never occur outside pathological data) and FAILS CLOSED -- treated
    as exhausted, never as unlimited age merely because the anchor is
    missing.
    """
    if attempts >= policy.max_attempts:
        return False
    if first_failure_at is None:
        return attempts == 0
    return (now - first_failure_at).total_seconds() < policy.max_retry_age


def _safe_failure_evidence(failure):
    """Bounded safe machine code + fixed controlled safe human reason --
    never the remote transport's own exception/response/ACK text
    (Corrective B Task 8 §9/§27)."""
    code = failure.error_code if failure.error_code else ""
    reason = _SAFE_FAILURE_REASONS.get(failure.category, "delivery failure")
    return code, reason


def _candidate_delay(failure, new_attempts, policy, random_source):
    """Prefer a valid Task-6 Retry-After over jitter for RATE_LIMITED,
    capped to policy.max_delay BEFORE any timestamp arithmetic -- an
    arbitrary-precision Retry-After integer (Task 6 I2) compares safely
    against a float via `min()` without ever being converted to float
    itself, so this can never overflow. Malformed/absent Retry-After
    (None) falls back to full jitter, exactly like any other retryable
    category."""
    if failure.category is SyncOutcome.RATE_LIMITED and failure.retry_after_seconds is not None:
        return float(min(failure.retry_after_seconds, policy.max_delay))
    return compute_retry_delay(new_attempts, base=policy.base_delay, cap=policy.max_delay, random_source=random_source)


def record_delivery_failure(
    claimed_row, failure, *, policy, now=None, random_source=None, keep_claim=False
):
    """The single authoritative, fenced writer for one failed-send
    outcome against exactly one claimed row (approved spec §§13-15,
    Corrective B Task 8). Re-resolves the row under
    `select_for_update()` inside a short transaction, matching the
    EXACT event_id, state=IN_FLIGHT, claim_token, AND captured
    `attempts` all at once -- the attempts comparison is what stops two
    reports derived from the SAME stale claimed snapshot from ever
    counting twice, even if the row's claim_token happened to survive
    (e.g. two in-process callers racing on the same in-memory row
    object). If the row no longer matches, this makes ZERO mutation and
    returns None.

    On a match: attempts increments exactly once; first_failure_at is
    set only if it was not already set (never replaced); safe failure
    evidence (category/http_status/bounded code/fixed reason) is always
    recorded. PERMANENT_REJECTION and AUTH_OR_REGISTRATION_FAILURE are
    immediately terminal. TRANSIENT_FAILURE and RATE_LIMITED are
    terminal only once EITHER budget is exhausted; otherwise the row
    returns to PENDING with `next_attempt_at = min(now + delay,
    first_failure_at + policy.max_retry_age)` so a due time can never
    itself authorize a send past the age deadline.

    `keep_claim=True` is PRIVATE orchestration for a failed multi-event
    413 send only: a surviving (budget-remaining) row keeps its exact
    IN_FLIGHT state/claim/lease untouched so the caller can immediately
    reuse it for a smaller resend without re-claiming; an exhausted row
    still terminalizes and clears its claim regardless.

    Returns the freshly read row, or None for stale fencing.
    """
    now = now or timezone.now()
    random_source = random_source if random_source is not None else random.random

    with transaction.atomic():
        try:
            current = OutboxMessage.objects.select_for_update().get(
                event_id=claimed_row.event_id,
                state=OUTBOX_IN_FLIGHT,
                claim_token=claimed_row.claim_token,
                attempts=claimed_row.attempts,
            )
        except OutboxMessage.DoesNotExist:
            return None

        new_attempts = current.attempts + 1
        first_failure_at = current.first_failure_at or now
        safe_code, safe_reason = _safe_failure_evidence(failure)

        changes = {
            "attempts": new_attempts,
            "first_failure_at": first_failure_at,
            "last_failure_category": failure.category.value,
            "last_http_status": failure.http_status,
            "rejection_code": safe_code,
            "rejection_reason": safe_reason,
        }

        terminal = failure.category in _TERMINAL_CATEGORIES
        if not terminal:
            terminal = not retry_budget_remaining(
                new_attempts, first_failure_at, policy=policy, now=now
            )

        if terminal:
            changes.update(
                state=OUTBOX_DEAD_LETTER, claimed_at=None, lease_until=None, claim_token=None
            )
        elif not keep_claim:
            delay = _candidate_delay(failure, new_attempts, policy, random_source)
            deadline = first_failure_at + timedelta(seconds=policy.max_retry_age)
            next_attempt_at = min(now + timedelta(seconds=delay), deadline)
            changes.update(
                state=OUTBOX_PENDING,
                next_attempt_at=next_attempt_at,
                claimed_at=None,
                lease_until=None,
                claim_token=None,
            )
        # else: keep_claim=True and budget remains -- state/claimed_at/
        # lease_until/claim_token stay exactly as they are (still
        # IN_FLIGHT under the same token) so the 413 orchestration below
        # can reuse this exact claim for a smaller resend.

        _write_immutable_row_fields(current, **changes)
        return OutboxMessage.objects.get(pk=current.pk)


def claim_outbox_batch(*, max_events, max_bytes, lease_seconds, now=None):
    """Claim a bounded batch of due, BUDGET-ELIGIBLE OutboxMessage rows:
    due PENDING rows, plus IN_FLIGHT rows whose lease has expired (a
    prior worker crashed or was killed mid-delivery). A candidate whose
    retry budget is already exhausted is terminalized to dead_letter in
    this SAME short transaction -- never claimed, never sent, never
    counted as a failed attempt (approved spec §15: "a preflight
    eligibility terminalization, not a failed send"). Selection stops at
    max_events, or before a row's canonical envelope bytes would push
    the running total over max_bytes -- except the batch always
    includes at least one row when any candidate exists, so a single
    oversized event can never starve itself forever; a second oversized
    row simply remains PENDING and eligible for a later call, never
    silently marked delivered or dead-lettered.

    Byte-budget accounting uses serialize_sync_batch() -- the SAME
    function HttpSyncTransport uses to build the actual outgoing
    request body -- so the locally enforced count can never mismatch
    what Central actually receives on the wire (corrective, independent
    review finding #4).

    Runs entirely inside one short transaction with
    select_for_update(skip_locked=True); performs no network I/O.
    Malformed retry configuration raises before any row is touched.
    """
    from .contracts import serialize_sync_batch

    policy = _current_retry_policy()
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
            if not retry_budget_remaining(
                row.attempts, row.first_failure_at, policy=policy, now=now
            ):
                # Preflight eligibility terminalization: exhausted BEFORE
                # ever being claimed for a send. Never a failed send --
                # no attempts increment, no failure evidence, no network.
                _bulk_write_immutable_rows(
                    OutboxMessage.objects.filter(pk=row.pk),
                    state=OUTBOX_DEAD_LETTER,
                    claimed_at=None,
                    lease_until=None,
                    claim_token=None,
                )
                continue
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
    returned; a stale/reclaimed row's write is fenced to zero rows.
    Retains all retry-cycle evidence (attempts/first_failure_at/last
    failure metadata) unchanged -- only Task 9's explicit requeue resets
    a retry cycle."""
    now = now or timezone.now()
    for row in claimed_rows:
        _write_fenced(row, state=OUTBOX_DELIVERED, delivered_at=now)


def mark_dead_letter(claimed_row, code, reason, now=None):
    """Compatibility adapter: delegates to record_delivery_failure() as
    a PERMANENT_REJECTION failure for exactly one event. `code` becomes
    the bounded safe rejection_code (e.g. a Central-supplied per-event
    ACK rejection code, already part of the established wire contract);
    `reason` is accepted for call-site compatibility but is never
    persisted verbatim -- record_delivery_failure() always writes its
    own fixed, safe reason text, never arbitrary remote-supplied text."""
    del reason
    policy = _current_retry_policy()
    failure = DeliveryFailure(category=SyncOutcome.PERMANENT_REJECTION, error_code=code)
    record_delivery_failure(claimed_row, failure, policy=policy, now=now)


def release_transient(claimed_rows, error_kind, now=None):
    """Compatibility adapter: delegates to record_delivery_failure() as
    a TRANSIENT_FAILURE for each row. `error_kind` is accepted for
    call-site compatibility only. Each row counts one failed attempt;
    it returns to PENDING with a persisted due time only while budget
    remains, otherwise it terminalizes to dead_letter -- transient
    failures are no longer retried unconditionally at any attempt count."""
    del error_kind
    policy = _current_retry_policy()
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)
    for row in claimed_rows:
        record_delivery_failure(row, failure, policy=policy, now=now)


def release_auth_failure(claimed_rows, now=None):
    """Compatibility adapter: delegates to record_delivery_failure() as
    an AUTH_OR_REGISTRATION_FAILURE for each row. A channel/credential
    fault is now immediately terminal (dead_letter) for each attempted
    row, counted once -- it is no longer released back to PENDING for
    unconditional automatic retry."""
    policy = _current_retry_policy()
    failure = DeliveryFailure(category=SyncOutcome.AUTH_OR_REGISTRATION_FAILURE)
    for row in claimed_rows:
        record_delivery_failure(row, failure, policy=policy, now=now)


@dataclass(frozen=True)
class FlushSummary:
    """The outcome of one flush_outbox() cycle -- owns no business
    mutation and no Central projection; purely a report of what
    happened to claimed Outbox rows."""

    claimed: int = 0
    delivered: int = 0
    dead_lettered: int = 0
    released_transient: int = 0
    # Corrective B Task 8: auth faults are no longer released to PENDING
    # for automatic retry (they terminalize instead), so this is always
    # zero; retained only for FlushSummary field compatibility.
    released_auth_failure: int = 0
    auth_fault: bool = False
    # A permanent/protocol rejection (e.g. HTTP 400) still flags operator
    # attention here, even though it no longer triggers bisection/retry.
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


def _record_batch_failure(rows, failure, policy, now):
    """Shared per-row accounting for a whole-batch transport failure
    (auth, non-413 protocol/permanent, or transient) -- consumes ONLY
    `failure.category`, never an HTTP status number or exception class,
    for the retry decision. Each row is recorded independently through
    record_delivery_failure(); a stale-fenced row contributes nothing
    (already claimed/resolved by another worker)."""
    dead_lettered = 0
    released_transient = 0
    for row in rows:
        updated = record_delivery_failure(row, failure, policy=policy, now=now)
        if updated is None:
            continue
        if updated.state == OUTBOX_DEAD_LETTER:
            dead_lettered += 1
        else:
            released_transient += 1

    return FlushSummary(
        claimed=len(rows),
        dead_lettered=dead_lettered,
        released_transient=released_transient,
        auth_fault=failure.category is SyncOutcome.AUTH_OR_REGISTRATION_FAILURE,
        protocol_fault=failure.category is SyncOutcome.PERMANENT_REJECTION,
    )


_EVENT_TOO_LARGE_FAILURE = DeliveryFailure(
    category=SyncOutcome.PERMANENT_REJECTION, error_code="EVENT_TOO_LARGE"
)


def _recover_from_request_too_large(transport, rows, failure, policy, now):
    """T14-D40: a batch of `rows` was rejected 413 as one failed send.
    Every row in that send counts exactly one failed attempt via
    `keep_claim=True`; a row whose budget is now exhausted terminalizes
    immediately and never participates in a smaller resend. Surviving
    rows (fresh, freshly-read instances -- never the stale in-memory
    ones) are bisected and each half is resent independently through
    _send_claimed_rows(), until a half shrinks to exactly one row and
    STILL receives 413: Central has now rejected that exact event
    alone, not merely the combination it originally shared a request
    with, so it is reclassified PERMANENT_REJECTION/EVENT_TOO_LARGE and
    terminalizes as its own separate failed send. Bounded by simple
    halving -- O(log n) deep for any realistic batch, never unbounded.
    """
    if len(rows) == 1:
        updated = record_delivery_failure(
            rows[0], _EVENT_TOO_LARGE_FAILURE, policy=policy, now=now
        )
        if updated is None:
            return FlushSummary(claimed=1)
        return FlushSummary(claimed=1, dead_lettered=1, protocol_fault=False)

    survivors = []
    dead_lettered = 0
    for row in rows:
        updated = record_delivery_failure(row, failure, policy=policy, now=now, keep_claim=True)
        if updated is None:
            continue
        if updated.state == OUTBOX_DEAD_LETTER:
            dead_lettered += 1
        else:
            survivors.append(updated)

    exhausted_here = FlushSummary(claimed=len(rows) - len(survivors), dead_lettered=dead_lettered)
    if not survivors:
        return exhausted_here

    midpoint = max(len(survivors) // 2, 1)
    first_half = _send_claimed_rows(transport, survivors[:midpoint], now=now, policy=policy)
    second_half = _send_claimed_rows(transport, survivors[midpoint:], now=now, policy=policy)
    return _merge_flush_summaries(exhausted_here, _merge_flush_summaries(first_half, second_half))


def _send_claimed_rows(transport, rows, now=None, policy=None):
    """Send exactly `rows` (already-claimed OutboxMessage instances)
    through `transport`, OUTSIDE any open DB transaction, then update
    delivery metadata only for confirmed events.

    Every transport-classified failure is consumed exclusively through
    `error.failure.category` (never an HTTP status number, never
    `isinstance` on a specific exception subclass beyond the one needed
    to detect the 413 split opportunity). PERMANENT_REJECTION and
    AUTH_OR_REGISTRATION_FAILURE terminalize every attempted row after
    counting one failed attempt each -- no bisection, no isolation.
    RequestTooLargeError (413) retains its own bounded split/singleton
    recovery, since a smaller request may still succeed where the
    original did not.
    """
    if not rows:
        return FlushSummary()
    if policy is None:
        policy = _current_retry_policy()

    envelopes = tuple(envelope_for(row) for row in rows)

    try:
        batch_ack = transport.send_batch(envelopes)
    except RequestTooLargeError as exc:
        return _recover_from_request_too_large(transport, rows, exc.failure, policy, now)
    except (AuthTransportError, ProtocolTransportError, TransientTransportError) as exc:
        return _record_batch_failure(rows, exc.failure, policy, now)

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
    rejected_with_code = []
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
            rejected_with_code.append((row, ack))
        else:
            unconfirmed_rows.append(row)

    if delivered_rows:
        mark_delivered(delivered_rows, now=now)

    dead_lettered = 0
    for row, ack in rejected_with_code:
        mark_dead_letter(row, ack.code, ack.reason, now=now)
        dead_lettered += 1

    released_transient = 0
    if unconfirmed_rows:
        transient_failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)
        for row in unconfirmed_rows:
            updated = record_delivery_failure(row, transient_failure, policy=policy, now=now)
            if updated is None:
                continue
            if updated.state == OUTBOX_DEAD_LETTER:
                dead_lettered += 1
            else:
                released_transient += 1

    return FlushSummary(
        claimed=len(rows),
        delivered=len(delivered_rows),
        dead_lettered=dead_lettered,
        released_transient=released_transient,
    )


def flush_outbox(transport, *, max_events, max_bytes, lease_seconds, now=None):
    """Claim a bounded, budget-eligible batch, send it through
    `transport` OUTSIDE any open DB transaction, then update delivery
    metadata only for confirmed events (T14-D08, T14-D16, T14-D17,
    T14-D40). Never reserializes from current domain state -- every
    envelope sent is exactly what claim_outbox_batch() returned. Owns
    no business mutation and no Central projection.
    """
    claimed = claim_outbox_batch(
        max_events=max_events, max_bytes=max_bytes, lease_seconds=lease_seconds, now=now
    )
    policy = _current_retry_policy()
    return _send_claimed_rows(transport, list(claimed), now=now, policy=policy)
