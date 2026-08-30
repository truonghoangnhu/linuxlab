"""Task 14 flush_outbox() orchestration (T14-D08, T14-D16, T14-D17):
claim -> send outside any open transaction -> update delivery metadata
only for confirmed event IDs. Owns no business mutation and no Central
projection.
"""

import uuid

import pytest
from django.utils import timezone

from linuxlab.sync.contracts import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    AuthTransportError,
    BatchAck,
    EventAck,
    ProtocolTransportError,
    RequestTooLargeError,
    TransientTransportError,
)
from linuxlab.sync.models import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_DELIVERED,
    OUTBOX_PENDING,
    OutboxMessage,
    _insert_immutable_row,
)
from linuxlab.sync.outbox import flush_outbox
from linuxlab.sync.transports import FileSyncTransport

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


class _ScriptedTransport:
    """A fake SyncTransport whose send_batch() outcome is scripted per
    test, so flush_outbox()'s handling of each classified outcome can be
    tested without any real network."""

    def __init__(self, *, result=None, exception=None):
        self._result = result
        self._exception = exception
        self.calls = []

    def send_batch(self, envelopes):
        self.calls.append(envelopes)
        if self._exception is not None:
            raise self._exception
        return self._result


class _CallableTransport:
    """A fake SyncTransport whose send_batch() outcome is decided by a
    caller-supplied function of the envelope batch itself -- needed for
    T14-D40 tests where the transport must behave differently depending
    on how many events are in the batch it receives (e.g. 413 for a
    multi-event batch, success for the same event sent alone)."""

    def __init__(self, decide):
        self._decide = decide
        self.calls = []

    def send_batch(self, envelopes):
        self.calls.append(envelopes)
        return self._decide(envelopes)


# --- successful flush --------------------------------------------------


def test_successful_flush_marks_accepted_and_duplicate_delivered():
    a = _make_outbox()
    b = _make_outbox(next_attempt_at=a.next_attempt_at)
    now = timezone.now()
    transport = _ScriptedTransport(
        result=BatchAck(
            results=(
                EventAck(event_id=a.event_id, status=ACCEPTED),
                EventAck(event_id=b.event_id, status=DUPLICATE),
            )
        )
    )

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now)

    assert summary.claimed == 2
    assert summary.delivered == 2
    refreshed_a = OutboxMessage.objects.get(pk=a.pk)
    refreshed_b = OutboxMessage.objects.get(pk=b.pk)
    assert refreshed_a.state == OUTBOX_DELIVERED
    assert refreshed_a.delivered_at == now
    assert refreshed_b.state == OUTBOX_DELIVERED
    assert refreshed_a.event_id == a.event_id
    assert refreshed_a.payload == a.payload


def test_flush_with_no_due_rows_is_a_safe_no_op():
    transport = _ScriptedTransport(result=BatchAck(results=()))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.claimed == 0
    assert transport.calls == []


# --- partial / missing ACK -------------------------------------------------


def test_missing_ack_returns_that_event_to_pending_not_delivered():
    a = _make_outbox()
    b = _make_outbox(next_attempt_at=a.next_attempt_at)
    c = _make_outbox(next_attempt_at=a.next_attempt_at)
    transport = _ScriptedTransport(
        result=BatchAck(
            results=(
                EventAck(event_id=a.event_id, status=ACCEPTED),
                EventAck(event_id=b.event_id, status=ACCEPTED),
            )
        )
    )

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.delivered == 2
    assert summary.released_transient == 1
    refreshed_c = OutboxMessage.objects.get(pk=c.pk)
    assert refreshed_c.state == OUTBOX_PENDING
    assert refreshed_c.attempts == 1
    assert refreshed_c.next_attempt_at > timezone.now()


# --- permanent rejection ----------------------------------------------------


def test_permanent_rejection_only_dead_letters_that_event():
    good = _make_outbox()
    poison = _make_outbox(next_attempt_at=good.next_attempt_at)
    transport = _ScriptedTransport(
        result=BatchAck(
            results=(
                EventAck(event_id=good.event_id, status=ACCEPTED),
                EventAck(
                    event_id=poison.event_id,
                    status=REJECTED,
                    code="IDEMPOTENCY_CONFLICT",
                    reason="conflict",
                ),
            )
        )
    )

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.delivered == 1
    assert summary.dead_lettered == 1
    refreshed_good = OutboxMessage.objects.get(pk=good.pk)
    refreshed_poison = OutboxMessage.objects.get(pk=poison.pk)
    assert refreshed_good.state == OUTBOX_DELIVERED
    assert refreshed_poison.state == OUTBOX_DEAD_LETTER
    assert refreshed_poison.rejection_code == "IDEMPOTENCY_CONFLICT"


# --- transient / timeout / 5xx ----------------------------------------------


def test_transient_transport_error_releases_whole_batch_with_incremented_attempts():
    a = _make_outbox(attempts=50_000)
    b = _make_outbox(attempts=50_000, next_attempt_at=a.next_attempt_at)
    transport = _ScriptedTransport(exception=TransientTransportError("timeout"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.released_transient == 2
    assert summary.dead_lettered == 0
    for row in (a, b):
        refreshed = OutboxMessage.objects.get(pk=row.pk)
        assert refreshed.state == OUTBOX_PENDING
        assert refreshed.attempts == 50_001


def test_protocol_transport_error_also_releases_as_transient_never_dead_letters():
    row = _make_outbox()
    transport = _ScriptedTransport(exception=ProtocolTransportError("request too large"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.released_transient == 1
    assert summary.dead_lettered == 0
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING


# --- 401/403 channel fault ---------------------------------------------------


def test_auth_transport_error_releases_whole_batch_without_dead_lettering_or_incrementing_attempts():
    a = _make_outbox(attempts=3)
    b = _make_outbox(attempts=3, next_attempt_at=a.next_attempt_at)
    transport = _ScriptedTransport(exception=AuthTransportError("401"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.auth_fault is True
    assert summary.released_auth_failure == 2
    assert summary.dead_lettered == 0
    for row in (a, b):
        refreshed = OutboxMessage.objects.get(pk=row.pk)
        assert refreshed.state == OUTBOX_PENDING
        assert refreshed.attempts == 3


# --- T14-D40: deterministic HTTP 413 recovery -------------------------------


def test_413_on_multi_event_batch_splits_and_delivers_good_events():
    a = _make_outbox()
    b = _make_outbox(next_attempt_at=a.next_attempt_at)
    c = _make_outbox(next_attempt_at=a.next_attempt_at)

    def _decide(envelopes):
        if len(envelopes) > 1:
            raise RequestTooLargeError("413")
        (envelope,) = envelopes
        return BatchAck(results=(EventAck(event_id=envelope.event_id, status=ACCEPTED),))

    transport = _CallableTransport(_decide)

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.claimed == 3
    assert summary.delivered == 3
    assert summary.dead_lettered == 0
    for row in (a, b, c):
        assert OutboxMessage.objects.get(pk=row.pk).state == OUTBOX_DELIVERED
    # More than one send_batch() call, eventually down to single-event
    # sends -- proving the split actually happened rather than merely
    # retrying the identical oversized batch.
    assert len(transport.calls) > 1
    assert any(len(call) == 1 for call in transport.calls)


def test_413_on_singleton_dead_letters_with_event_too_large():
    row = _make_outbox()
    transport = _ScriptedTransport(exception=RequestTooLargeError("413"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.claimed == 1
    assert summary.dead_lettered == 1
    assert summary.delivered == 0
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_DEAD_LETTER
    assert refreshed.rejection_code == "EVENT_TOO_LARGE"


def test_413_split_isolates_the_oversized_singleton_and_delivers_its_neighbor():
    good = _make_outbox()
    oversized = _make_outbox(next_attempt_at=good.next_attempt_at)

    def _decide(envelopes):
        ids = {e.event_id for e in envelopes}
        if oversized.event_id in ids and len(envelopes) == 1:
            raise RequestTooLargeError("413 even alone")
        if len(envelopes) > 1:
            raise RequestTooLargeError("413")
        (envelope,) = envelopes
        return BatchAck(results=(EventAck(event_id=envelope.event_id, status=ACCEPTED),))

    transport = _CallableTransport(_decide)

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.claimed == 2
    assert summary.delivered == 1
    assert summary.dead_lettered == 1
    assert OutboxMessage.objects.get(pk=good.pk).state == OUTBOX_DELIVERED
    refreshed_oversized = OutboxMessage.objects.get(pk=oversized.pk)
    assert refreshed_oversized.state == OUTBOX_DEAD_LETTER
    assert refreshed_oversized.rejection_code == "EVENT_TOO_LARGE"


def test_413_split_never_recurses_unboundedly_for_a_realistic_batch():
    first = _make_outbox()
    rows = [first] + [_make_outbox(next_attempt_at=first.next_attempt_at) for _ in range(7)]
    transport = _ScriptedTransport(exception=RequestTooLargeError("413"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    # Every claimed event, split all the way down to singletons, still
    # rejected as too large -- all 8 dead-letter, none delivered, and
    # nothing hangs or raises a recursion error.
    assert summary.claimed == 8
    assert summary.dead_lettered == 8
    for row in rows:
        assert OutboxMessage.objects.get(pk=row.pk).state == OUTBOX_DEAD_LETTER


# --- T14-D40: deterministic HTTP 400 handling --------------------------------


def test_deterministic_400_releases_retryable_without_dead_lettering():
    a = _make_outbox()
    b = _make_outbox(next_attempt_at=a.next_attempt_at)
    transport = _ScriptedTransport(exception=ProtocolTransportError("malformed request"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.protocol_fault is True
    assert summary.dead_lettered == 0
    assert summary.delivered == 0
    assert summary.released_transient == 2
    for row in (a, b):
        refreshed = OutboxMessage.objects.get(pk=row.pk)
        assert refreshed.state == OUTBOX_PENDING
        assert refreshed.attempts == 1


def test_deterministic_400_does_not_tight_loop_the_identical_request():
    row = _make_outbox()
    transport = _ScriptedTransport(exception=ProtocolTransportError("malformed request"))
    now = timezone.now()

    first = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now)
    refreshed_after_first = OutboxMessage.objects.get(pk=row.pk)

    # The SAME malformed request would recur unchanged on an immediate
    # retry -- but next_attempt_at has moved out via backoff, so an
    # immediate second flush at the same `now` claims nothing.
    second = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now)

    assert first.protocol_fault is True
    assert second.claimed == 0
    assert refreshed_after_first.next_attempt_at > now


# --- FileSyncTransport non-delivery -----------------------------------------


def test_file_transport_flush_leaves_events_unconfirmed_never_delivered(tmp_path):
    row = _make_outbox()
    transport = FileSyncTransport(tmp_path)

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.delivered == 0
    assert summary.released_transient == 1
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING
    assert refreshed.state != OUTBOX_DELIVERED
    assert list(tmp_path.glob("*.json"))
