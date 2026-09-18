"""Corrective B Task 8 (approved spec §§13-15): bounded sync retry
policy -- full-jitter exponential backoff, attempt+age budget, fenced
failure recording as the sole retry authority, and worker claim
eligibility. Integration-level coverage of the outcome-driven dispatch
(413 split/singleton, whole-batch 400/401/timeout handling, ACK
processing) lives in test_task14_flush.py; the compatibility-wrapper
surface (mark_delivered/mark_dead_letter/release_transient/
release_auth_failure) and claim/lease mechanics live in
test_task14_outbox.py. This file covers the new policy primitives
themselves: compute_retry_delay(), retry_budget_remaining(), and
record_delivery_failure() as the sole fenced writer.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from linuxlab.sync.configuration import SyncRetryPolicy
from linuxlab.sync.contracts import DeliveryFailure, SyncOutcome
from linuxlab.sync.models import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_IN_FLIGHT,
    OUTBOX_PENDING,
    OutboxMessage,
    _bulk_write_immutable_rows,
    _insert_immutable_row,
)
from linuxlab.sync.outbox import (
    _revalidate_claimed_for_send,
    claim_outbox_batch,
    compute_retry_delay,
    record_delivery_failure,
    retry_budget_remaining,
)

pytestmark = pytest.mark.django_db

_POLICY = SyncRetryPolicy(base_delay=5.0, max_delay=3600.0, max_attempts=3, max_retry_age=100.0)


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
        "state": OUTBOX_IN_FLIGHT,
        "attempts": 0,
        "next_attempt_at": now,
        "claimed_at": now,
        "lease_until": now + timedelta(minutes=5),
        "claim_token": uuid.uuid4(),
        "delivered_at": None,
        "rejection_code": "",
        "rejection_reason": "",
    }
    fields.update(overrides)
    return _insert_immutable_row(OutboxMessage, **fields)


# --- compute_retry_delay(): full-jitter exponential backoff -----------------


def test_first_failure_upper_jitter_is_base():
    assert (
        compute_retry_delay(1, base=5.0, cap=3600.0, random_source=lambda: 1.0) == 5.0
    )


def test_compute_retry_delay_doubles_per_subsequent_attempt_before_cap():
    assert compute_retry_delay(1, base=1.0, cap=1000.0, random_source=lambda: 1.0) == 1.0
    assert compute_retry_delay(2, base=1.0, cap=1000.0, random_source=lambda: 1.0) == 2.0
    assert compute_retry_delay(3, base=1.0, cap=1000.0, random_source=lambda: 1.0) == 4.0
    assert compute_retry_delay(4, base=1.0, cap=1000.0, random_source=lambda: 1.0) == 8.0


def test_compute_retry_delay_respects_cap():
    assert compute_retry_delay(30, base=1.0, cap=100.0, random_source=lambda: 1.0) == 100.0


def test_compute_retry_delay_jitter_scales_the_ceiling():
    full = compute_retry_delay(3, base=1.0, cap=1000.0, random_source=lambda: 1.0)
    half = compute_retry_delay(3, base=1.0, cap=1000.0, random_source=lambda: 0.5)
    zero = compute_retry_delay(3, base=1.0, cap=1000.0, random_source=lambda: 0.0)

    assert half == full / 2
    assert zero == 0.0
    assert 0 <= half <= full


def test_compute_retry_delay_never_raises_at_astronomically_large_attempt_counts():
    delay = compute_retry_delay(10**18, base=1.0, cap=3600.0, random_source=lambda: 1.0)
    assert delay == 3600.0


def test_compute_retry_delay_does_not_truncate_at_a_fixed_exponent_for_a_tiny_base():
    # A base needing WELL more than 64 doublings to reach cap must still
    # reach the correct ceiling -- never silently under-delay via an
    # arbitrary exponent-64 truncation.
    delay = compute_retry_delay(2000, base=1e-300, cap=3600.0, random_source=lambda: 1.0)
    assert delay == 3600.0


def test_compute_retry_delay_zero_and_one_attempts_are_equivalent_first_ceiling():
    # attempts=0 is defensively treated the same as attempts=1 (n=0).
    assert compute_retry_delay(0, base=5.0, cap=3600.0, random_source=lambda: 1.0) == 5.0


# --- retry_budget_remaining(): bounded attempts + bounded age ---------------


def test_retry_budget_remaining_true_before_attempt_ceiling():
    now = timezone.now()
    assert retry_budget_remaining(2, now, policy=_POLICY, now=now) is True


def test_retry_budget_remaining_false_at_attempt_ceiling_boundary():
    now = timezone.now()
    assert retry_budget_remaining(3, now, policy=_POLICY, now=now) is False


def test_retry_budget_remaining_true_just_under_age_ceiling():
    now = timezone.now()
    first_failure_at = now - timedelta(seconds=99)
    assert retry_budget_remaining(1, first_failure_at, policy=_POLICY, now=now) is True


def test_retry_budget_remaining_false_at_age_ceiling_boundary():
    now = timezone.now()
    first_failure_at = now - timedelta(seconds=100)
    assert retry_budget_remaining(1, first_failure_at, policy=_POLICY, now=now) is False


def test_retry_budget_remaining_true_for_untouched_row():
    now = timezone.now()
    assert retry_budget_remaining(0, None, policy=_POLICY, now=now) is True


def test_retry_budget_remaining_fails_closed_for_invalid_attempts_without_anchor():
    # attempts > 0 with no first_failure_at is an invalid persisted
    # state -- must fail closed (exhausted), never unlimited age.
    now = timezone.now()
    assert retry_budget_remaining(1, None, policy=_POLICY, now=now) is False


# --- record_delivery_failure(): the sole fenced failure-recording authority -


def test_record_delivery_failure_increments_attempts_and_sets_first_failure_at_once():
    row = _make_outbox(attempts=0, first_failure_at=None)
    now = timezone.now()
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    assert updated.attempts == 1
    assert updated.first_failure_at == now
    assert updated.last_failure_category == "TRANSIENT_FAILURE"
    assert updated.state == OUTBOX_PENDING


def test_record_delivery_failure_never_replaces_an_existing_first_failure_at():
    original_first_failure = timezone.now() - timedelta(seconds=30)
    row = _make_outbox(attempts=1, first_failure_at=original_first_failure)
    now = timezone.now()
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    assert updated.first_failure_at == original_first_failure
    assert updated.attempts == 2


def test_record_delivery_failure_returns_none_and_writes_nothing_for_stale_fencing():
    row = _make_outbox(attempts=0, claim_token=uuid.uuid4())
    # A different claim_token means another worker has already reclaimed
    # this exact row -- the stale caller's write must be a safe no-op.
    row.claim_token = uuid.uuid4()
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    result = record_delivery_failure(row, failure, policy=_POLICY)

    assert result is None
    persisted = OutboxMessage.objects.get(pk=row.pk)
    assert persisted.attempts == 0


def test_record_delivery_failure_returns_none_when_attempts_snapshot_is_stale():
    row = _make_outbox(attempts=0)
    # Simulate a second, concurrent report against the SAME claim_token
    # but a stale in-memory attempts snapshot (the first report already
    # incremented attempts in the DB).
    _bulk_write_immutable_rows(OutboxMessage.objects.filter(pk=row.pk), attempts=1)
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    result = record_delivery_failure(row, failure, policy=_POLICY)

    assert result is None
    assert OutboxMessage.objects.get(pk=row.pk).attempts == 1


@pytest.mark.parametrize(
    "category",
    [SyncOutcome.PERMANENT_REJECTION, SyncOutcome.AUTH_OR_REGISTRATION_FAILURE],
)
def test_record_delivery_failure_terminalizes_immediately_regardless_of_budget(category):
    # Even on the FIRST attempt (budget nowhere near exhausted), these
    # categories are unconditionally terminal.
    row = _make_outbox(attempts=0)
    failure = DeliveryFailure(category=category)

    updated = record_delivery_failure(row, failure, policy=_POLICY)

    assert updated.state == OUTBOX_DEAD_LETTER
    assert updated.claim_token is None
    assert updated.claimed_at is None
    assert updated.lease_until is None


@pytest.mark.parametrize("category", [SyncOutcome.TRANSIENT_FAILURE, SyncOutcome.RATE_LIMITED])
def test_record_delivery_failure_retries_within_budget_for_retryable_categories(category):
    row = _make_outbox(attempts=0)
    failure = DeliveryFailure(category=category)

    updated = record_delivery_failure(row, failure, policy=_POLICY)

    assert updated.state == OUTBOX_PENDING
    assert updated.next_attempt_at > timezone.now()


@pytest.mark.parametrize("category", [SyncOutcome.TRANSIENT_FAILURE, SyncOutcome.RATE_LIMITED])
def test_record_delivery_failure_terminalizes_once_budget_is_exhausted(category):
    row = _make_outbox(attempts=_POLICY.max_attempts - 1, first_failure_at=timezone.now())
    failure = DeliveryFailure(category=category)

    updated = record_delivery_failure(row, failure, policy=_POLICY)

    assert updated.state == OUTBOX_DEAD_LETTER
    assert updated.attempts == _POLICY.max_attempts


def test_record_delivery_failure_writes_only_safe_fixed_evidence_never_raw_text():
    row = _make_outbox(attempts=0)
    failure = DeliveryFailure(category=SyncOutcome.PERMANENT_REJECTION, http_status=400)

    updated = record_delivery_failure(row, failure, policy=_POLICY)

    assert "raw" not in updated.rejection_reason.lower()
    assert updated.rejection_reason == "rejected by Central as a permanent failure"
    assert updated.last_http_status == 400


def test_record_delivery_failure_keep_claim_retains_claim_when_budget_remains():
    original_token = uuid.uuid4()
    now = timezone.now()
    row = _make_outbox(
        attempts=0, claim_token=original_token, claimed_at=now, lease_until=now + timedelta(minutes=5)
    )
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    updated = record_delivery_failure(row, failure, policy=_POLICY, keep_claim=True)

    assert updated.state == OUTBOX_IN_FLIGHT
    assert updated.claim_token == original_token
    assert updated.claimed_at == now
    assert updated.attempts == 1


def test_record_delivery_failure_keep_claim_still_terminalizes_and_clears_claim_when_exhausted():
    row = _make_outbox(
        attempts=_POLICY.max_attempts - 1, first_failure_at=timezone.now(), claim_token=uuid.uuid4()
    )
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    updated = record_delivery_failure(row, failure, policy=_POLICY, keep_claim=True)

    assert updated.state == OUTBOX_DEAD_LETTER
    assert updated.claim_token is None
    assert updated.claimed_at is None
    assert updated.lease_until is None


def test_record_delivery_failure_due_time_never_exceeds_the_age_deadline(monkeypatch):
    # A long computed delay must be clamped to the remaining age budget,
    # never scheduling a due time past first_failure_at + max_retry_age.
    first_failure_at = timezone.now() - timedelta(seconds=95)
    policy = SyncRetryPolicy(base_delay=1000.0, max_delay=3600.0, max_attempts=100, max_retry_age=100.0)
    row = _make_outbox(attempts=0, first_failure_at=first_failure_at)
    now = timezone.now()
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    monkeypatch.setattr("linuxlab.sync.outbox.random.random", lambda: 1.0)
    updated = record_delivery_failure(row, failure, policy=policy, now=now)

    assert updated.state == OUTBOX_PENDING
    assert updated.next_attempt_at == first_failure_at + timedelta(seconds=policy.max_retry_age)


def test_record_delivery_failure_rate_limited_prefers_retry_after_over_jitter(monkeypatch):
    row = _make_outbox(attempts=0)
    now = timezone.now()
    failure = DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=42)

    monkeypatch.setattr("linuxlab.sync.outbox.random.random", lambda: 1.0)
    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    assert updated.next_attempt_at == now + timedelta(seconds=42)


def test_record_delivery_failure_rate_limited_caps_a_huge_retry_after_before_arithmetic():
    row = _make_outbox(attempts=0)
    now = timezone.now()
    # An arbitrary-precision Retry-After value (Task 6 I2) -- must be
    # capped to policy.max_delay BEFORE any timedelta() arithmetic, or
    # this would raise OverflowError.
    failure = DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=10**400)

    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    assert updated.state == OUTBOX_PENDING
    assert updated.next_attempt_at <= now + timedelta(seconds=_POLICY.max_delay)


def test_record_delivery_failure_rate_limited_falls_back_to_jitter_when_retry_after_absent(
    monkeypatch,
):
    row = _make_outbox(attempts=0)
    now = timezone.now()
    failure = DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=None)

    monkeypatch.setattr("linuxlab.sync.outbox.random.random", lambda: 1.0)
    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    # n=0 for the first attempt -- ceiling is base_delay via jitter.
    assert updated.next_attempt_at == now + timedelta(seconds=_POLICY.base_delay)


# --- claim_outbox_batch(): preflight budget eligibility ---------------------


def test_claim_terminalizes_an_exhausted_candidate_without_claiming_or_counting_it(settings):
    settings.LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS = 3
    now = timezone.now()
    exhausted = _make_outbox(
        state=OUTBOX_PENDING,
        attempts=3,
        first_failure_at=now - timedelta(seconds=10),
        next_attempt_at=now,
        claim_token=None,
        claimed_at=None,
        lease_until=None,
    )

    claimed = claim_outbox_batch(max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now)

    assert claimed == ()
    refreshed = OutboxMessage.objects.get(pk=exhausted.pk)
    assert refreshed.state == OUTBOX_DEAD_LETTER
    # A preflight terminalization is NOT a failed send -- it must never
    # increment attempts or touch failure evidence.
    assert refreshed.attempts == 3
    assert refreshed.last_failure_category == ""
    assert refreshed.claim_token is None


def test_claim_still_selects_a_budget_eligible_candidate(settings):
    settings.LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS = 3
    now = timezone.now()
    eligible = _make_outbox(
        state=OUTBOX_PENDING,
        attempts=1,
        first_failure_at=now - timedelta(seconds=10),
        next_attempt_at=now,
        claim_token=None,
        claimed_at=None,
        lease_until=None,
    )

    claimed = claim_outbox_batch(max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now)

    assert [row.event_id for row in claimed] == [eligible.event_id]
    assert OutboxMessage.objects.get(pk=eligible.pk).state == OUTBOX_IN_FLIGHT


# --- Corrective B Task 8 Fix Round 1 ----------------------------------------
# I1: pre-send authority/budget recheck; I2: safe bounded persisted machine
# code; I4: missing first-failure anchor fails closed; I5: datetime
# representability can never erase a real failed send.

def _in_flight_pending_budget(**overrides):
    return _make_outbox(**overrides)


# I1 -- _revalidate_claimed_for_send(): the short-transaction recheck run
# immediately before EVERY network send (initial batch and every 413 child).


def test_presend_recheck_keeps_a_row_whose_claim_and_budget_are_intact():
    now = timezone.now()
    row = _in_flight_pending_budget(attempts=1, first_failure_at=now - timedelta(seconds=50))

    survivors, dead_lettered = _revalidate_claimed_for_send([row], policy=_POLICY, now=now)

    assert dead_lettered == 0
    assert [r.event_id for r in survivors] == [row.event_id]
    assert survivors[0].claim_token == row.claim_token
    assert survivors[0].attempts == 1
    assert OutboxMessage.objects.get(pk=row.pk).state == OUTBOX_IN_FLIGHT


def test_presend_recheck_terminalizes_at_exact_age_equality_without_counting_or_sending():
    now = timezone.now()
    row = _in_flight_pending_budget(
        attempts=1, first_failure_at=now - timedelta(seconds=_POLICY.max_retry_age)
    )

    survivors, dead_lettered = _revalidate_claimed_for_send([row], policy=_POLICY, now=now)

    assert survivors == ()
    assert dead_lettered == 1
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_DEAD_LETTER
    assert refreshed.attempts == 1  # a prevented send is never a failed send
    assert refreshed.last_failure_category == ""
    assert refreshed.claim_token is None
    assert refreshed.claimed_at is None
    assert refreshed.lease_until is None


def test_presend_recheck_keeps_a_row_one_second_under_the_age_ceiling():
    now = timezone.now()
    row = _in_flight_pending_budget(
        attempts=1, first_failure_at=now - timedelta(seconds=_POLICY.max_retry_age - 1)
    )

    survivors, dead_lettered = _revalidate_claimed_for_send([row], policy=_POLICY, now=now)

    assert dead_lettered == 0
    assert len(survivors) == 1


def test_presend_recheck_terminalizes_at_exact_attempts_equality():
    now = timezone.now()
    row = _in_flight_pending_budget(attempts=_POLICY.max_attempts, first_failure_at=now)

    survivors, dead_lettered = _revalidate_claimed_for_send([row], policy=_POLICY, now=now)

    assert survivors == ()
    assert dead_lettered == 1
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_DEAD_LETTER
    assert refreshed.attempts == _POLICY.max_attempts


def test_presend_recheck_excludes_a_row_whose_claim_token_changed_with_zero_mutation():
    now = timezone.now()
    row = _in_flight_pending_budget(attempts=0)
    new_token = uuid.uuid4()
    _bulk_write_immutable_rows(OutboxMessage.objects.filter(pk=row.pk), claim_token=new_token)
    before = OutboxMessage.objects.values().get(pk=row.pk)

    survivors, dead_lettered = _revalidate_claimed_for_send([row], policy=_POLICY, now=now)

    assert survivors == ()
    assert dead_lettered == 0
    assert OutboxMessage.objects.values().get(pk=row.pk) == before


def test_presend_recheck_excludes_a_row_whose_attempts_snapshot_is_stale():
    now = timezone.now()
    row = _in_flight_pending_budget(attempts=0)
    _bulk_write_immutable_rows(OutboxMessage.objects.filter(pk=row.pk), attempts=1)
    before = OutboxMessage.objects.values().get(pk=row.pk)

    survivors, dead_lettered = _revalidate_claimed_for_send([row], policy=_POLICY, now=now)

    assert survivors == ()
    assert dead_lettered == 0
    assert OutboxMessage.objects.values().get(pk=row.pk) == before


def test_presend_recheck_excludes_a_row_no_longer_in_flight():
    now = timezone.now()
    row = _in_flight_pending_budget(attempts=0)
    _bulk_write_immutable_rows(OutboxMessage.objects.filter(pk=row.pk), state=OUTBOX_PENDING)
    before = OutboxMessage.objects.values().get(pk=row.pk)

    survivors, dead_lettered = _revalidate_claimed_for_send([row], policy=_POLICY, now=now)

    assert survivors == ()
    assert dead_lettered == 0
    assert OutboxMessage.objects.values().get(pk=row.pk) == before


def test_presend_recheck_fails_closed_for_missing_anchor_without_counting():
    now = timezone.now()
    row = _in_flight_pending_budget(attempts=1, first_failure_at=None)

    survivors, dead_lettered = _revalidate_claimed_for_send([row], policy=_POLICY, now=now)

    assert survivors == ()
    assert dead_lettered == 1
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_DEAD_LETTER
    assert refreshed.attempts == 1
    assert refreshed.first_failure_at is None


def test_presend_recheck_returns_only_surviving_siblings_in_original_order():
    now = timezone.now()
    keep_a = _in_flight_pending_budget(attempts=1, first_failure_at=now - timedelta(seconds=10))
    expired = _in_flight_pending_budget(
        attempts=1, first_failure_at=now - timedelta(seconds=_POLICY.max_retry_age)
    )
    keep_b = _in_flight_pending_budget(attempts=1, first_failure_at=now - timedelta(seconds=10))

    survivors, dead_lettered = _revalidate_claimed_for_send(
        [keep_a, expired, keep_b], policy=_POLICY, now=now
    )

    assert dead_lettered == 1
    assert [r.event_id for r in survivors] == [keep_a.event_id, keep_b.event_id]
    assert OutboxMessage.objects.get(pk=expired.pk).state == OUTBOX_DEAD_LETTER
    assert OutboxMessage.objects.get(pk=keep_a.pk).state == OUTBOX_IN_FLIGHT


# I2 -- persisted rejection/failure machine code is normalized through ONE
# private boundary: bounded, conservative token grammar, fixed safe fallback.


def test_authorization_shaped_remote_code_is_never_persisted_raw_but_still_terminal():
    row = _make_outbox(attempts=0)
    failure = DeliveryFailure(
        category=SyncOutcome.PERMANENT_REJECTION,
        error_code="Bearer AUTH_SENTINEL_TOKEN_VALUE",
    )

    updated = record_delivery_failure(row, failure, policy=_POLICY)

    assert updated.state == OUTBOX_DEAD_LETTER
    assert updated.attempts == 1
    persisted = OutboxMessage.objects.values().get(pk=row.pk)
    assert "AUTH_SENTINEL_TOKEN_VALUE" not in str(persisted)
    assert "Bearer" not in str(persisted)
    assert persisted["rejection_code"] != ""
    assert len(persisted["rejection_code"]) <= 100


def test_oversized_remote_code_never_raises_data_error_and_never_erases_accounting():
    row = _make_outbox(attempts=0)
    oversized = "A" * 101
    failure = DeliveryFailure(category=SyncOutcome.PERMANENT_REJECTION, error_code=oversized)

    updated = record_delivery_failure(row, failure, policy=_POLICY)

    assert updated.state == OUTBOX_DEAD_LETTER
    assert updated.attempts == 1
    assert updated.last_failure_category == "PERMANENT_REJECTION"
    assert updated.rejection_code != oversized
    assert updated.rejection_code != ""
    assert len(updated.rejection_code) <= 100
    # Never a silent truncation of the remote text either.
    assert updated.rejection_code != oversized[:100]


def test_ordinary_safe_machine_code_is_preserved_exactly():
    row = _make_outbox(attempts=0)
    failure = DeliveryFailure(
        category=SyncOutcome.PERMANENT_REJECTION, error_code="IDEMPOTENCY_CONFLICT"
    )

    updated = record_delivery_failure(row, failure, policy=_POLICY)

    assert updated.rejection_code == "IDEMPOTENCY_CONFLICT"
    assert updated.state == OUTBOX_DEAD_LETTER


@pytest.mark.parametrize(
    "unsafe_code",
    [
        "Authorization: Bearer x",
        "{\"body\": \"text\"}",
        "free form reason text",
        "code\nwith\nnewlines",
        "lower_case_code",
        "_LEADING_UNDERSCORE",
        "1STARTS_WITH_DIGIT",
        "B" * 100 + "C",
    ],
)
def test_every_unsafe_code_shape_maps_to_the_same_fixed_fallback(unsafe_code):
    row = _make_outbox(attempts=0)
    failure = DeliveryFailure(category=SyncOutcome.PERMANENT_REJECTION, error_code=unsafe_code)

    updated = record_delivery_failure(row, failure, policy=_POLICY)

    assert updated.rejection_code == "UNSAFE_REMOTE_CODE"
    assert updated.attempts == 1
    assert updated.state == OUTBOX_DEAD_LETTER


def test_empty_code_stays_empty_for_transient_failures():
    row = _make_outbox(attempts=0)
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    updated = record_delivery_failure(row, failure, policy=_POLICY)

    assert updated.rejection_code == ""


# I4 -- attempts > 0 with first_failure_at None is an invalid persisted retry
# cycle: a real failed send is still counted exactly once, but the missing
# anchor is NEVER renewed by assigning now; the row terminalizes instead.


def test_record_delivery_failure_never_renews_a_missing_anchor_and_terminalizes():
    row = _make_outbox(attempts=1, first_failure_at=None)
    now = timezone.now()
    original_next_attempt_at = row.next_attempt_at
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    assert updated.attempts == 2
    assert updated.first_failure_at is None
    assert updated.state == OUTBOX_DEAD_LETTER
    assert updated.last_failure_category == "TRANSIENT_FAILURE"
    assert updated.claim_token is None
    assert updated.claimed_at is None
    assert updated.lease_until is None
    assert updated.next_attempt_at == original_next_attempt_at


def test_record_delivery_failure_missing_anchor_keep_claim_still_terminalizes():
    row = _make_outbox(attempts=1, first_failure_at=None)
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    updated = record_delivery_failure(row, failure, policy=_POLICY, keep_claim=True)

    assert updated.attempts == 2
    assert updated.first_failure_at is None
    assert updated.state == OUTBOX_DEAD_LETTER
    assert updated.claim_token is None


def test_record_delivery_failure_untouched_cycle_still_anchors_on_first_real_failure():
    row = _make_outbox(attempts=0, first_failure_at=None)
    now = timezone.now()
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)

    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    assert updated.attempts == 1
    assert updated.first_failure_at == now
    assert updated.state == OUTBOX_PENDING


# I5 -- datetime representability: an unrepresentable due time/deadline
# fails closed AFTER accounting, never as an escaping OverflowError that
# rolls the failed send back.


_NEAR_DATETIME_MAX = datetime.max.replace(tzinfo=UTC) - timedelta(seconds=2)


def test_unrepresentable_due_time_fails_closed_after_counting_the_failed_send(monkeypatch):
    now = _NEAR_DATETIME_MAX
    row = _make_outbox(attempts=0, first_failure_at=None)
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)
    monkeypatch.setattr("linuxlab.sync.outbox.random.random", lambda: 1.0)

    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    assert updated.attempts == 1
    assert updated.first_failure_at == now
    assert updated.last_failure_category == "TRANSIENT_FAILURE"
    assert updated.state == OUTBOX_DEAD_LETTER
    assert updated.claim_token is None
    assert updated.claimed_at is None
    assert updated.lease_until is None


def test_unrepresentable_retry_after_schedule_fails_closed_after_counting():
    now = _NEAR_DATETIME_MAX
    row = _make_outbox(attempts=0, first_failure_at=None)
    failure = DeliveryFailure(category=SyncOutcome.RATE_LIMITED, retry_after_seconds=30)

    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    assert updated.attempts == 1
    assert updated.state == OUTBOX_DEAD_LETTER
    assert updated.last_failure_category == "RATE_LIMITED"


def test_unrepresentable_age_deadline_fails_closed_after_counting(monkeypatch):
    # Due time itself is representable (zero jitter) but the age deadline
    # first_failure_at + max_retry_age is not.
    now = _NEAR_DATETIME_MAX
    row = _make_outbox(attempts=0, first_failure_at=None)
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)
    monkeypatch.setattr("linuxlab.sync.outbox.random.random", lambda: 0.0)

    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    assert updated.attempts == 1
    assert updated.state == OUTBOX_DEAD_LETTER


def test_representable_near_boundary_schedule_still_returns_to_pending(monkeypatch):
    now = datetime.max.replace(tzinfo=UTC) - timedelta(minutes=10)
    row = _make_outbox(attempts=0, first_failure_at=None)
    failure = DeliveryFailure(category=SyncOutcome.TRANSIENT_FAILURE)
    monkeypatch.setattr("linuxlab.sync.outbox.random.random", lambda: 1.0)

    updated = record_delivery_failure(row, failure, policy=_POLICY, now=now)

    assert updated.attempts == 1
    assert updated.state == OUTBOX_PENDING
    assert updated.next_attempt_at == now + timedelta(seconds=_POLICY.base_delay)
