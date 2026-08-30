"""Task 14 Outbox claim/lease/retry/delivery state machine
(T14-D13, T14-D14, T14-D15, T14-D16, T14-D17).

claim_outbox_batch() is the only place that reads PENDING/lease-expired
rows and transitions them to IN_FLIGHT, under select_for_update(
skip_locked=True), inside a short transaction -- it never performs
network I/O. mark_delivered()/mark_dead_letter()/release_transient()/
release_auth_failure() are the only controlled writers of Outbox
delivery metadata; none of them may touch the immutable wire-envelope
fields (event_id through payload).
"""

import threading
import uuid
from datetime import timedelta

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from linuxlab.sync.contracts import canonical_event_bytes
from linuxlab.sync.models import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_DELIVERED,
    OUTBOX_IN_FLIGHT,
    OUTBOX_PENDING,
    OutboxMessage,
    _bulk_write_immutable_rows,
    _insert_immutable_row,
)
from linuxlab.sync.outbox import (
    claim_outbox_batch,
    compute_retry_delay,
    envelope_for,
    mark_dead_letter,
    mark_delivered,
    release_auth_failure,
    release_transient,
)

pytestmark = pytest.mark.django_db


def _make_outbox(**overrides):
    now = timezone.now()
    fields = {
        "event_id": uuid.uuid4(),
        "domain_event": None,
        "event_type": "activity.completed",
        "schema_version": 1,
        "source_node_id": uuid.uuid4(),
        "occurred_at": now,
        "aggregate_type": "attempt",
        "aggregate_public_id": uuid.uuid4(),
        "payload": {"score": 1, "max_score": 1},
        "state": OUTBOX_PENDING,
        "attempts": 0,
        "next_attempt_at": now,
        "claimed_at": None,
        "lease_until": None,
        "delivered_at": None,
        "rejection_code": "",
        "rejection_reason": "",
    }
    fields.update(overrides)
    return _insert_immutable_row(OutboxMessage, **fields)


# --- claim_outbox_batch() ----------------------------------------------------


def test_claim_only_selects_due_pending_rows():
    future = timezone.now() + timedelta(hours=1)
    due = _make_outbox()
    not_due = _make_outbox(next_attempt_at=future)
    delivered = _make_outbox(state=OUTBOX_DELIVERED, delivered_at=timezone.now())
    dead = _make_outbox(state=OUTBOX_DEAD_LETTER, rejection_code="X", rejection_reason="x")

    claimed = claim_outbox_batch(max_events=10, max_bytes=1_000_000, lease_seconds=60)

    claimed_ids = {row.event_id for row in claimed}
    assert claimed_ids == {due.event_id}
    assert not_due.event_id not in claimed_ids
    assert delivered.event_id not in claimed_ids
    assert dead.event_id not in claimed_ids


def test_claim_orders_by_next_attempt_at_then_created_at_then_pk():
    now = timezone.now()
    third = _make_outbox(next_attempt_at=now)
    first = _make_outbox(next_attempt_at=now - timedelta(seconds=10))
    second = _make_outbox(next_attempt_at=now - timedelta(seconds=5))

    claimed = claim_outbox_batch(max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert [row.event_id for row in claimed] == [first.event_id, second.event_id, third.event_id]


def test_claim_transitions_selected_rows_to_in_flight_with_lease():
    row = _make_outbox()
    now = row.next_attempt_at

    claim_outbox_batch(max_events=10, max_bytes=1_000_000, lease_seconds=120, now=now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_IN_FLIGHT
    assert refreshed.claimed_at == now
    assert refreshed.lease_until == now + timedelta(seconds=120)


def test_claim_reclaims_lease_expired_in_flight_row_without_changing_envelope():
    now = timezone.now()
    row = _make_outbox(
        state=OUTBOX_IN_FLIGHT,
        claimed_at=now - timedelta(hours=1),
        lease_until=now - timedelta(minutes=1),
    )

    claimed = claim_outbox_batch(max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now)

    assert [c.event_id for c in claimed] == [row.event_id]
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_IN_FLIGHT
    assert refreshed.event_id == row.event_id
    assert refreshed.payload == row.payload
    assert refreshed.aggregate_public_id == row.aggregate_public_id


def test_claim_does_not_reclaim_row_with_active_lease():
    now = timezone.now()
    _make_outbox(
        state=OUTBOX_IN_FLIGHT,
        claimed_at=now,
        lease_until=now + timedelta(minutes=5),
    )

    claimed = claim_outbox_batch(max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now)

    assert claimed == ()


def test_claim_stops_at_max_events():
    for _ in range(5):
        _make_outbox()

    claimed = claim_outbox_batch(max_events=3, max_bytes=1_000_000, lease_seconds=60)

    assert len(claimed) == 3
    assert OutboxMessage.objects.filter(state=OUTBOX_PENDING).count() == 2


def test_claim_stops_before_exceeding_byte_budget_and_leaves_remainder_pending():
    small = _make_outbox(payload={"x": 1})
    small_bytes = len(canonical_event_bytes(envelope_for(small)))
    _make_outbox(payload={"x": 1}, next_attempt_at=small.next_attempt_at + timedelta(seconds=1))

    claimed = claim_outbox_batch(
        max_events=10, max_bytes=small_bytes, lease_seconds=60
    )

    assert len(claimed) == 1
    assert claimed[0].event_id == small.event_id
    assert OutboxMessage.objects.filter(state=OUTBOX_PENDING).count() == 1


# --- corrective (independent review finding #4): byte accounting must match
# the actual wire request body exactly, via the one shared serializer.


def test_claim_byte_budget_uses_wrapped_batch_bytes_not_raw_per_event_sum():
    from linuxlab.sync.contracts import canonical_event_bytes

    first = _make_outbox(payload={"x": 1})
    second = _make_outbox(
        payload={"x": 1}, next_attempt_at=first.next_attempt_at + timedelta(seconds=1)
    )
    raw_per_event_bytes = len(canonical_event_bytes(envelope_for(first)))
    # What a naive per-event-sum (the pre-corrective bug) would have
    # allowed for two events -- the real wrapped `{"events":[...]}` body
    # is strictly larger due to wrapper/separator overhead, so the
    # correct accounting must NOT fit both under this budget.
    naive_budget = raw_per_event_bytes * 2

    claimed = claim_outbox_batch(
        max_events=10, max_bytes=naive_budget, lease_seconds=60, now=second.next_attempt_at
    )

    assert len(claimed) == 1
    assert claimed[0].event_id == first.event_id


def test_claim_byte_budget_exact_boundary_fits_exactly():
    from linuxlab.sync.contracts import serialize_sync_batch

    first = _make_outbox(payload={"x": 1})
    second = _make_outbox(
        payload={"x": 1}, next_attempt_at=first.next_attempt_at + timedelta(seconds=1)
    )
    exact_bytes = len(serialize_sync_batch([envelope_for(first), envelope_for(second)]))

    claimed = claim_outbox_batch(
        max_events=10, max_bytes=exact_bytes, lease_seconds=60, now=second.next_attempt_at
    )

    assert {c.event_id for c in claimed} == {first.event_id, second.event_id}


def test_claim_byte_budget_one_byte_under_exact_boundary_excludes_last_event():
    from linuxlab.sync.contracts import serialize_sync_batch

    first = _make_outbox(payload={"x": 1})
    second = _make_outbox(
        payload={"x": 1}, next_attempt_at=first.next_attempt_at + timedelta(seconds=1)
    )
    exact_bytes = len(serialize_sync_batch([envelope_for(first), envelope_for(second)]))

    claimed = claim_outbox_batch(
        max_events=10, max_bytes=exact_bytes - 1, lease_seconds=60, now=second.next_attempt_at
    )

    assert len(claimed) == 1
    assert claimed[0].event_id == first.event_id


def test_claim_always_includes_at_least_one_oversized_row_without_marking_it_delivered():
    row = _make_outbox(payload={"x": "y" * 500})

    claimed = claim_outbox_batch(max_events=10, max_bytes=1, lease_seconds=60)

    assert [c.event_id for c in claimed] == [row.event_id]
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_IN_FLIGHT


def test_claim_second_oversized_row_remains_pending_and_eligible():
    now = timezone.now()
    first = _make_outbox(payload={"x": "y" * 500}, next_attempt_at=now)
    second = _make_outbox(payload={"x": "y" * 500}, next_attempt_at=now + timedelta(seconds=1))

    claimed = claim_outbox_batch(max_events=10, max_bytes=1, lease_seconds=60, now=now)

    assert [c.event_id for c in claimed] == [first.event_id]
    refreshed_second = OutboxMessage.objects.get(pk=second.pk)
    assert refreshed_second.state == OUTBOX_PENDING
    assert refreshed_second.event_id == second.event_id


@pytest.mark.django_db(transaction=True)
def test_concurrent_claims_never_select_the_same_row():
    _make_outbox()
    _make_outbox()

    results = [None, None]
    barrier = threading.Barrier(2)

    class _ClaimWorker(threading.Thread):
        def __init__(self, index):
            super().__init__()
            self.index = index

        def run(self):
            close_old_connections()
            try:
                barrier.wait()
                claimed = claim_outbox_batch(max_events=1, max_bytes=1_000_000, lease_seconds=60)
                results[self.index] = tuple(row.event_id for row in claimed)
            finally:
                connection.close()

    barrier_workers = [_ClaimWorker(0), _ClaimWorker(1)]
    for worker in barrier_workers:
        worker.start()
    for worker in barrier_workers:
        worker.join()

    claimed_ids = results[0] + results[1]
    assert len(claimed_ids) == len(set(claimed_ids)) == 2
    assert OutboxMessage.objects.filter(state=OUTBOX_IN_FLIGHT).count() == 2


# --- real PostgreSQL stale-worker fencing (corrective, review finding #3) --
# Each phase below runs on its OWN real DB connection/thread -- not a mock --
# to prove the fencing is enforced by the actual claim_token comparison
# round-tripped through PostgreSQL, not merely by in-process bookkeeping.


class _RunOnOwnConnection(threading.Thread):
    def __init__(self, action, results, key):
        super().__init__()
        self.action = action
        self.results = results
        self.key = key

    def run(self):
        close_old_connections()
        try:
            self.results[self.key] = self.action()
        finally:
            connection.close()


@pytest.mark.django_db(transaction=True)
def test_stale_worker_cannot_resurrect_a_row_reclaimed_and_delivered_by_another_worker():
    now = timezone.now()
    outbox = _make_outbox(next_attempt_at=now)
    results = {}

    def _worker_a_claims():
        return claim_outbox_batch(max_events=10, max_bytes=10_000_000, lease_seconds=1, now=now)

    worker_a = _RunOnOwnConnection(_worker_a_claims, results, "a_claimed")
    worker_a.start()
    worker_a.join()
    stale_row = results["a_claimed"][0]

    later = now + timedelta(seconds=5)

    def _worker_b_reclaims_and_delivers():
        reclaimed = claim_outbox_batch(
            max_events=10, max_bytes=10_000_000, lease_seconds=60, now=later
        )
        mark_delivered(reclaimed, now=later)
        return reclaimed

    worker_b = _RunOnOwnConnection(_worker_b_reclaims_and_delivers, results, "b_claimed")
    worker_b.start()
    worker_b.join()
    assert results["b_claimed"][0].event_id == outbox.event_id
    assert results["b_claimed"][0].claim_token != stale_row.claim_token

    even_later = now + timedelta(seconds=10)

    def _worker_a_reports_transient_failure_too_late():
        release_transient([stale_row], "timeout", now=even_later)

    worker_a_late = _RunOnOwnConnection(
        _worker_a_reports_transient_failure_too_late, results, "a_late"
    )
    worker_a_late.start()
    worker_a_late.join()

    refreshed = OutboxMessage.objects.get(pk=outbox.pk)
    assert refreshed.state == OUTBOX_DELIVERED
    assert refreshed.event_id == outbox.event_id
    assert refreshed.payload == outbox.payload


@pytest.mark.django_db(transaction=True)
def test_stale_worker_success_report_affects_zero_rows_after_reclaim_released_it():
    now = timezone.now()
    outbox = _make_outbox(next_attempt_at=now)
    results = {}

    def _worker_a_claims():
        return claim_outbox_batch(max_events=10, max_bytes=10_000_000, lease_seconds=1, now=now)

    worker_a = _RunOnOwnConnection(_worker_a_claims, results, "a_claimed")
    worker_a.start()
    worker_a.join()
    stale_row = results["a_claimed"][0]

    later = now + timedelta(seconds=5)

    def _worker_b_reclaims_and_dead_letters():
        reclaimed = claim_outbox_batch(
            max_events=10, max_bytes=10_000_000, lease_seconds=60, now=later
        )
        mark_dead_letter(reclaimed[0], "IDEMPOTENCY_CONFLICT", "central rejected it", now=later)
        return reclaimed

    worker_b = _RunOnOwnConnection(_worker_b_reclaims_and_dead_letters, results, "b_claimed")
    worker_b.start()
    worker_b.join()

    even_later = now + timedelta(seconds=10)

    def _worker_a_reports_success_too_late():
        mark_delivered([stale_row], now=even_later)

    worker_a_late = _RunOnOwnConnection(_worker_a_reports_success_too_late, results, "a_late")
    worker_a_late.start()
    worker_a_late.join()

    refreshed = OutboxMessage.objects.get(pk=outbox.pk)
    assert refreshed.state == OUTBOX_DEAD_LETTER
    assert refreshed.rejection_code == "IDEMPOTENCY_CONFLICT"
    assert refreshed.delivered_at is None


# --- mark_delivered() --------------------------------------------------------


def test_mark_delivered_transitions_accepted_and_duplicate_rows():
    now = timezone.now()
    accepted = _make_outbox(state=OUTBOX_IN_FLIGHT, claimed_at=now, lease_until=now + timedelta(minutes=5))
    duplicate = _make_outbox(state=OUTBOX_IN_FLIGHT, claimed_at=now, lease_until=now + timedelta(minutes=5))

    mark_delivered([accepted, duplicate], now=now)

    for row in (accepted, duplicate):
        refreshed = OutboxMessage.objects.get(pk=row.pk)
        assert refreshed.state == OUTBOX_DELIVERED
        assert refreshed.delivered_at == now


def test_mark_delivered_never_touches_envelope_fields():
    row = _make_outbox(state=OUTBOX_IN_FLIGHT)

    mark_delivered([row])

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.event_id == row.event_id
    assert refreshed.payload == row.payload
    assert refreshed.event_type == row.event_type


# --- mark_dead_letter() ------------------------------------------------------


def test_mark_dead_letter_transitions_only_the_named_event():
    now = timezone.now()
    poison = _make_outbox(state=OUTBOX_IN_FLIGHT)
    healthy = _make_outbox(state=OUTBOX_IN_FLIGHT)

    mark_dead_letter(poison, "IDEMPOTENCY_CONFLICT", "conflicting content", now=now)

    refreshed_poison = OutboxMessage.objects.get(pk=poison.pk)
    assert refreshed_poison.state == OUTBOX_DEAD_LETTER
    assert refreshed_poison.rejection_code == "IDEMPOTENCY_CONFLICT"
    assert refreshed_poison.rejection_reason == "conflicting content"

    refreshed_healthy = OutboxMessage.objects.get(pk=healthy.pk)
    assert refreshed_healthy.state == OUTBOX_IN_FLIGHT


# --- release_transient() -----------------------------------------------------


def test_release_transient_returns_row_to_pending_with_incremented_attempts():
    now = timezone.now()
    row = _make_outbox(state=OUTBOX_IN_FLIGHT, attempts=2, claimed_at=now, lease_until=now + timedelta(minutes=5))

    release_transient([row], "timeout", now=now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING
    assert refreshed.attempts == 3
    assert refreshed.claimed_at is None
    assert refreshed.lease_until is None
    assert refreshed.next_attempt_at > now


def test_release_transient_never_dead_letters_at_any_attempt_count():
    row = _make_outbox(state=OUTBOX_IN_FLIGHT, attempts=10_000)

    release_transient([row], "timeout")

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING


# --- release_auth_failure() --------------------------------------------------


def test_release_auth_failure_returns_row_to_pending_without_incrementing_attempts():
    now = timezone.now()
    row = _make_outbox(state=OUTBOX_IN_FLIGHT, attempts=1, claimed_at=now, lease_until=now + timedelta(minutes=5))

    release_auth_failure([row], now=now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING
    assert refreshed.attempts == 1
    assert refreshed.claimed_at is None
    assert refreshed.lease_until is None


def test_release_auth_failure_never_dead_letters():
    row = _make_outbox(state=OUTBOX_IN_FLIGHT)

    release_auth_failure([row])

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING


# --- corrective (independent review finding #3): stale-claim fencing --------
# claim_outbox_batch() mints a fresh claim_token per row; every writer must
# fence its update to state=IN_FLIGHT + that exact token, so a caller still
# holding an in-memory row object from BEFORE a reclaim can never affect the
# row again -- its write must match zero rows, not raise, not silently
# "succeed" against a row it no longer owns.


def test_mark_delivered_after_reclaim_affects_zero_rows_and_keeps_new_claim_state():
    stale = _make_outbox(state=OUTBOX_IN_FLIGHT, claim_token=uuid.uuid4())
    new_token = uuid.uuid4()
    _bulk_write_immutable_rows(OutboxMessage.objects.filter(pk=stale.pk), claim_token=new_token)
    reclaimed = OutboxMessage.objects.get(pk=stale.pk)
    # A different worker delivers under the NEW claim first.
    mark_delivered([reclaimed])

    # The stale worker's in-memory `stale` object still carries the OLD
    # claim_token; its late write must affect nothing.
    mark_delivered([stale])

    refreshed = OutboxMessage.objects.get(pk=stale.pk)
    assert refreshed.state == OUTBOX_DELIVERED
    assert refreshed.claim_token == new_token


def test_release_transient_after_reclaim_affects_zero_rows():
    stale = _make_outbox(state=OUTBOX_IN_FLIGHT, attempts=0, claim_token=uuid.uuid4())
    new_token = uuid.uuid4()
    _bulk_write_immutable_rows(
        OutboxMessage.objects.filter(pk=stale.pk), claim_token=new_token, attempts=5
    )
    reclaimed = OutboxMessage.objects.get(pk=stale.pk)
    mark_delivered([reclaimed])

    release_transient([stale], "timeout")

    refreshed = OutboxMessage.objects.get(pk=stale.pk)
    assert refreshed.state == OUTBOX_DELIVERED
    assert refreshed.attempts == 5


def test_mark_dead_letter_after_reclaim_affects_zero_rows():
    stale = _make_outbox(state=OUTBOX_IN_FLIGHT, claim_token=uuid.uuid4())
    new_token = uuid.uuid4()
    _bulk_write_immutable_rows(OutboxMessage.objects.filter(pk=stale.pk), claim_token=new_token)
    reclaimed = OutboxMessage.objects.get(pk=stale.pk)
    mark_delivered([reclaimed])

    mark_dead_letter(stale, "IDEMPOTENCY_CONFLICT", "should never apply")

    refreshed = OutboxMessage.objects.get(pk=stale.pk)
    assert refreshed.state == OUTBOX_DELIVERED
    assert refreshed.rejection_code == ""


# --- compute_retry_delay() ---------------------------------------------------


def test_compute_retry_delay_grows_exponentially_before_cap():
    delay_1 = compute_retry_delay(1, base=1.0, cap=1000.0, random_source=lambda: 1.0)
    delay_2 = compute_retry_delay(2, base=1.0, cap=1000.0, random_source=lambda: 1.0)
    delay_3 = compute_retry_delay(3, base=1.0, cap=1000.0, random_source=lambda: 1.0)

    assert delay_1 == 2.0
    assert delay_2 == 4.0
    assert delay_3 == 8.0


def test_compute_retry_delay_respects_cap():
    delay = compute_retry_delay(30, base=1.0, cap=100.0, random_source=lambda: 1.0)

    assert delay == 100.0


def test_compute_retry_delay_jitter_changes_delay_within_bound():
    full = compute_retry_delay(3, base=1.0, cap=1000.0, random_source=lambda: 1.0)
    half = compute_retry_delay(3, base=1.0, cap=1000.0, random_source=lambda: 0.5)

    assert half == full / 2
    assert 0 <= half <= full


def test_compute_retry_delay_never_raises_at_large_attempt_counts():
    delay = compute_retry_delay(10_000, base=1.0, cap=3600.0, random_source=lambda: 1.0)

    assert delay == 3600.0


# --- envelope_for() -----------------------------------------------------------


def test_envelope_for_reflects_stored_immutable_fields():
    row = _make_outbox()

    envelope = envelope_for(row)

    assert envelope.event_id == row.event_id
    assert envelope.event_type == row.event_type
    assert envelope.schema_version == row.schema_version
    assert envelope.source_node_id == row.source_node_id
    assert envelope.aggregate_type == row.aggregate_type
    assert envelope.aggregate_public_id == row.aggregate_public_id
    assert dict(envelope.payload) == row.payload


# --- guard: writers reject unknown fields ------------------------------------


def test_fenced_outbox_write_rejects_non_mutable_field():
    row = _make_outbox(state=OUTBOX_IN_FLIGHT)

    with pytest.raises(ValueError):
        from linuxlab.sync.outbox import _write_fenced

        _write_fenced(row, payload={"tampered": True})
