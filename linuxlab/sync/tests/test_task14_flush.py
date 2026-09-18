"""Task 14 flush_outbox() orchestration (T14-D08, T14-D16, T14-D17):
claim -> send outside any open transaction -> update delivery metadata
only for confirmed event IDs. Owns no business mutation and no Central
projection.
"""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from linuxlab.sync.contracts import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    AuthTransportError,
    BatchAck,
    DeliveryFailure,
    EventAck,
    ProtocolTransportError,
    RequestTooLargeError,
    SyncOutcome,
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

# Corrective B Task 6: ProtocolTransportError now requires an explicit
# DeliveryFailure -- these existing fixtures all represent an HTTP-400-style
# protocol fault, so they carry PERMANENT_REJECTION metadata (the same
# category classify_http_failure(400) itself constructs). This is a
# constructor-only adaptation: it does not change any of these tests'
# pre-Task-8 worker assertions (outbox.py still dispatches by exception
# class, unchanged, until Task 8).
_HTTP_400_FAILURE = DeliveryFailure(category=SyncOutcome.PERMANENT_REJECTION, http_status=400)


def _protocol_error(message="400"):
    return ProtocolTransportError(message, failure=_HTTP_400_FAILURE)


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
    first_failure_at = timezone.now() - timedelta(minutes=1)
    a = _make_outbox(attempts=3, first_failure_at=first_failure_at)
    b = _make_outbox(
        attempts=3, first_failure_at=first_failure_at, next_attempt_at=a.next_attempt_at
    )
    transport = _ScriptedTransport(exception=TransientTransportError("timeout"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.released_transient == 2
    assert summary.dead_lettered == 0
    for row in (a, b):
        refreshed = OutboxMessage.objects.get(pk=row.pk)
        assert refreshed.state == OUTBOX_PENDING
        assert refreshed.attempts == 4


def test_transient_transport_error_dead_letters_once_the_retry_budget_is_exhausted():
    # Corrective B Task 8: transient failures are bounded -- once either
    # budget (attempts or age) is exhausted, the row terminalizes instead
    # of retrying unconditionally at any attempt count.
    a = _make_outbox(attempts=9, first_failure_at=timezone.now())
    transport = _ScriptedTransport(exception=TransientTransportError("timeout"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.released_transient == 0
    assert summary.dead_lettered == 1
    refreshed = OutboxMessage.objects.get(pk=a.pk)
    assert refreshed.state == OUTBOX_DEAD_LETTER
    assert refreshed.attempts == 10


def test_protocol_transport_error_dead_letters_the_whole_batch_immediately():
    # Corrective B Task 8: HTTP 400 (PERMANENT_REJECTION) is an
    # implementation-side fault, but it is no longer bisected/retried --
    # it dead-letters every claimed row in the batch immediately.
    a = _make_outbox()
    b = _make_outbox(next_attempt_at=a.next_attempt_at)
    transport = _ScriptedTransport(exception=_protocol_error("malformed request"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.protocol_fault is True
    assert summary.dead_lettered == 2
    assert summary.released_transient == 0
    for row in (a, b):
        refreshed = OutboxMessage.objects.get(pk=row.pk)
        assert refreshed.state == OUTBOX_DEAD_LETTER
        assert refreshed.attempts == 1
        assert refreshed.last_http_status == 400


# --- 401/403 channel fault ---------------------------------------------------


def test_auth_transport_error_dead_letters_the_whole_batch_with_one_counted_attempt():
    # Corrective B Task 8: a channel/credential fault is immediately
    # terminal for every attempted row -- it is no longer released back
    # to PENDING for unconditional automatic retry.
    first_failure_at = timezone.now() - timedelta(minutes=1)
    a = _make_outbox(attempts=3, first_failure_at=first_failure_at)
    b = _make_outbox(
        attempts=3, first_failure_at=first_failure_at, next_attempt_at=a.next_attempt_at
    )
    transport = _ScriptedTransport(exception=AuthTransportError("401"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.auth_fault is True
    assert summary.dead_lettered == 2
    assert summary.released_auth_failure == 0
    for row in (a, b):
        refreshed = OutboxMessage.objects.get(pk=row.pk)
        assert refreshed.state == OUTBOX_DEAD_LETTER
        assert refreshed.attempts == 4


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


# --- corrective (independent review finding #7): flush_outbox() itself is
# ambiguity-safe, even if handed a BatchAck a transport built by hand rather
# than through the shared parse_ack_results() parser.


def test_flush_treats_duplicate_event_id_in_batch_ack_as_unconfirmed():
    row = _make_outbox()
    transport = _ScriptedTransport(
        result=BatchAck(
            results=(
                EventAck(event_id=row.event_id, status=ACCEPTED),
                EventAck(event_id=row.event_id, status=ACCEPTED),
            )
        )
    )

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.delivered == 0
    assert summary.dead_lettered == 0
    assert summary.released_transient == 1
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING


def test_flush_treats_conflicting_entries_for_same_id_as_unconfirmed_never_dead_letters():
    row = _make_outbox()
    transport = _ScriptedTransport(
        result=BatchAck(
            results=(
                EventAck(event_id=row.event_id, status=ACCEPTED),
                EventAck(event_id=row.event_id, status=REJECTED, code="X"),
            )
        )
    )

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.delivered == 0
    assert summary.dead_lettered == 0
    assert summary.released_transient == 1
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING


def test_flush_treats_rejected_without_code_as_unconfirmed_never_dead_letters():
    row = _make_outbox()
    transport = _ScriptedTransport(
        result=BatchAck(results=(EventAck(event_id=row.event_id, status=REJECTED, code=""),))
    )

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.delivered == 0
    assert summary.dead_lettered == 0
    assert summary.released_transient == 1
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING


def test_flush_through_real_http_transport_never_delivers_a_masked_duplicate_id():
    """End-to-end through the REAL HttpSyncTransport response parser
    (parse_ack_results()), not a hand-built BatchAck: a malformed
    rejected-without-code entry must not mask that its event_id was
    also claimed by a valid `accepted` entry (second-corrective finding
    #3) -- the event stays PENDING, a real unrelated neighbor still
    delivers."""
    import json
    from unittest.mock import MagicMock, patch

    from linuxlab.sync.transports import HttpSyncTransport

    ambiguous = _make_outbox()
    good = _make_outbox(next_attempt_at=ambiguous.next_attempt_at)
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")

    def _fake_urlopen(request, timeout=None):
        del timeout
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(
            {
                "results": [
                    {"event_id": str(ambiguous.event_id), "status": ACCEPTED},
                    {"event_id": str(ambiguous.event_id), "status": REJECTED},
                    {"event_id": str(good.event_id), "status": ACCEPTED},
                ]
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    with patch("linuxlab.sync.transports._TRANSPORT_OPENER.open", side_effect=_fake_urlopen):
        summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.delivered == 1
    assert summary.dead_lettered == 0
    assert OutboxMessage.objects.get(pk=good.pk).state == OUTBOX_DELIVERED
    assert OutboxMessage.objects.get(pk=ambiguous.pk).state == OUTBOX_PENDING


# --- Corrective B Task 8: deterministic HTTP 400 dead-letters immediately ---
# 400 no longer bisects, isolates, or uses a long protocol-specific backoff
# (the old T14-D40/T14-D41 mechanism is removed entirely): PERMANENT_REJECTION
# is unconditionally terminal, so a whole-batch 400 dead-letters every
# claimed row in that one send, uniformly, on the first failure.


def test_deterministic_400_on_singleton_dead_letters_immediately():
    row = _make_outbox()
    transport = _ScriptedTransport(exception=_protocol_error("malformed request"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.protocol_fault is True
    assert summary.dead_lettered == 1
    assert summary.delivered == 0
    assert summary.released_transient == 0
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_DEAD_LETTER


def test_400_does_not_tight_loop_because_the_row_is_no_longer_eligible():
    _make_outbox()
    transport = _ScriptedTransport(exception=_protocol_error("malformed request"))
    now = timezone.now()

    first = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now)
    second = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now)

    assert first.protocol_fault is True
    assert first.dead_lettered == 1
    # The row is DEAD_LETTER now, so it is never claimed again -- no
    # second identical request is ever sent.
    assert second.claimed == 0
    assert len(transport.calls) == 1


def test_multi_event_400_dead_letters_every_claimed_event_uniformly():
    a = _make_outbox()
    b = _make_outbox(next_attempt_at=a.next_attempt_at)
    c = _make_outbox(next_attempt_at=a.next_attempt_at)
    d = _make_outbox(next_attempt_at=a.next_attempt_at)
    transport = _ScriptedTransport(exception=_protocol_error("400"))

    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.claimed == 4
    assert summary.delivered == 0
    assert summary.dead_lettered == 4
    assert summary.protocol_fault is True
    for row in (a, b, c, d):
        assert OutboxMessage.objects.get(pk=row.pk).state == OUTBOX_DEAD_LETTER
    # A single request, never bisected/retried.
    assert len(transport.calls) == 1


def test_unrelated_eligible_event_still_progresses_in_a_later_cycle_after_a_400():
    now = timezone.now()
    stuck = _make_outbox(next_attempt_at=now)
    # Not yet due for the first cycle -- claimed only in the second.
    unrelated = _make_outbox(next_attempt_at=now + timedelta(minutes=10))

    def _decide(envelopes):
        ids = {e.event_id for e in envelopes}
        if stuck.event_id in ids:
            raise _protocol_error("400")
        return BatchAck(
            results=tuple(EventAck(event_id=e.event_id, status=ACCEPTED) for e in envelopes)
        )

    transport = _CallableTransport(_decide)

    first = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now)
    assert first.dead_lettered == 1
    assert OutboxMessage.objects.get(pk=stuck.pk).state == OUTBOX_DEAD_LETTER

    later = now + timedelta(minutes=10)
    second = flush_outbox(
        transport, max_events=10, max_bytes=1_000_000, lease_seconds=60, now=later
    )
    assert second.delivered == 1
    assert OutboxMessage.objects.get(pk=unrelated.pk).state == OUTBOX_DELIVERED


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
