"""Corrective B Task 9 (approved spec §15): explicit operator outbox
requeue. requeue_outbox_message() is an atomic, Outbox-only state
transition -- it resets exactly one dead_letter row's retry cycle
(state, attempts, first_failure_at, next_attempt_at, claim/lease
metadata, and current-cycle failure evidence) and returns the refreshed
row. It never sends the event, never publishes a DomainEvent, never
creates another Outbox row, and never performs network I/O. Only a
dead_letter row may transition; every other state (or an unknown/
invalid event_id) is rejected with OutboxRequeueError and zero
mutation. Repository audit-pattern decision NO_EXISTING_PATTERN_EVENT_REMOVED
(spec §15): no new DomainEvent type is introduced for this.
"""

import threading
import uuid
from datetime import timedelta

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from linuxlab.core.models import DomainEvent
from linuxlab.sync.models import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_DELIVERED,
    OUTBOX_IN_FLIGHT,
    OUTBOX_PENDING,
    OutboxMessage,
    _insert_immutable_row,
)

pytestmark = pytest.mark.django_db


def _configure_worker_settings(settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(uuid.uuid4())
    settings.LINUXLAB_SYNC_SECRET = "the-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"


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


def _make_dead_letter_row(**overrides):
    domain_event = DomainEvent.objects.create(
        event_type="activity.completed",
        aggregate_id=uuid.uuid4(),
        payload={"score": 1, "max_score": 1},
    )
    now = timezone.now()
    fields = {
        "domain_event": domain_event,
        "state": OUTBOX_DEAD_LETTER,
        "attempts": 3,
        "first_failure_at": now - timedelta(hours=2),
        "last_failure_category": "PERMANENT_REJECTION",
        "last_http_status": 400,
        "rejection_code": "IDEMPOTENCY_CONFLICT",
        "rejection_reason": "rejected by Central as a permanent failure",
        "next_attempt_at": now - timedelta(hours=1),
        "claimed_at": None,
        "lease_until": None,
        "delivered_at": None,
    }
    fields.update(overrides)
    return _make_outbox(**fields)


def test_requeue_resets_only_outbox_cycle(settings):
    from linuxlab.sync.outbox import requeue_outbox_message

    _configure_worker_settings(settings)
    row = _make_dead_letter_row()

    before_events = list(DomainEvent.objects.values())
    before_ids = set(OutboxMessage.objects.values_list("event_id", flat=True))

    requeued = requeue_outbox_message(row.event_id)

    assert requeued.state == OUTBOX_PENDING
    assert requeued.attempts == 0
    assert requeued.first_failure_at is None
    assert requeued.domain_event_id == row.domain_event_id

    assert list(DomainEvent.objects.values()) == before_events
    assert set(OutboxMessage.objects.values_list("event_id", flat=True)) == before_ids


# --- immutable envelope + delivered_at history preservation -----------------


def test_requeue_preserves_the_complete_immutable_envelope_and_history(settings):
    from linuxlab.sync.outbox import requeue_outbox_message

    _configure_worker_settings(settings)
    delivered_at = timezone.now() - timedelta(days=1)
    row = _make_dead_letter_row(delivered_at=delivered_at)
    before = OutboxMessage.objects.values().get(pk=row.pk)

    requeue_outbox_message(row.event_id)

    after = OutboxMessage.objects.values().get(pk=row.pk)
    envelope_and_history_fields = (
        "event_id",
        "domain_event_id",
        "event_type",
        "schema_version",
        "source_node_id",
        "occurred_at",
        "aggregate_type",
        "aggregate_public_id",
        "payload",
        "created_at",
        "delivered_at",
    )
    for field_name in envelope_and_history_fields:
        assert after[field_name] == before[field_name], field_name
    assert after["delivered_at"] == delivered_at


# --- reset semantics ----------------------------------------------------------


def test_requeue_clears_claim_lease_and_token(settings):
    from linuxlab.sync.outbox import requeue_outbox_message

    _configure_worker_settings(settings)
    row = _make_dead_letter_row(
        claimed_at=timezone.now(), lease_until=timezone.now(), claim_token=uuid.uuid4()
    )

    requeued = requeue_outbox_message(row.event_id)

    assert requeued.claimed_at is None
    assert requeued.lease_until is None
    assert requeued.claim_token is None


def test_requeue_clears_current_cycle_failure_evidence(settings):
    from linuxlab.sync.outbox import requeue_outbox_message

    _configure_worker_settings(settings)
    row = _make_dead_letter_row()

    requeued = requeue_outbox_message(row.event_id)

    assert requeued.last_failure_category == ""
    assert requeued.last_http_status is None
    assert requeued.rejection_code == ""
    assert requeued.rejection_reason == ""


def test_requeue_exact_reset_timestamp_via_clock_seam(settings, monkeypatch):
    from linuxlab.sync import outbox as outbox_module

    _configure_worker_settings(settings)
    row = _make_dead_letter_row()
    frozen_now = timezone.now() + timedelta(hours=1)
    monkeypatch.setattr(outbox_module.timezone, "now", lambda: frozen_now)

    requeued = outbox_module.requeue_outbox_message(row.event_id)

    assert requeued.next_attempt_at == frozen_now
    assert requeued.next_attempt_at != row.next_attempt_at
    assert requeued.next_attempt_at != row.first_failure_at


# --- wrong-state coverage: only dead_letter may transition -------------------


@pytest.mark.parametrize("state", [OUTBOX_PENDING, OUTBOX_IN_FLIGHT, OUTBOX_DELIVERED])
def test_requeue_rejects_every_non_dead_letter_state(settings, state):
    from linuxlab.sync.outbox import OutboxRequeueError, requeue_outbox_message

    _configure_worker_settings(settings)
    row = _make_outbox(state=state, attempts=1)
    before = OutboxMessage.objects.values().get(pk=row.pk)
    before_events = list(DomainEvent.objects.values())

    with pytest.raises(OutboxRequeueError):
        requeue_outbox_message(row.event_id)

    after = OutboxMessage.objects.values().get(pk=row.pk)
    assert after == before
    assert list(DomainEvent.objects.values()) == before_events
    assert OutboxMessage.objects.count() == 1


def test_requeue_rejects_unknown_event_id(settings):
    from linuxlab.sync.outbox import OutboxRequeueError, requeue_outbox_message

    _configure_worker_settings(settings)
    before_ids = set(OutboxMessage.objects.values_list("event_id", flat=True))

    with pytest.raises(OutboxRequeueError):
        requeue_outbox_message(uuid.uuid4())

    assert set(OutboxMessage.objects.values_list("event_id", flat=True)) == before_ids


@pytest.mark.parametrize("bad_selector", ["not-a-uuid", 12345, None, b"1234", ["x"]])
def test_requeue_defensively_rejects_a_non_uuid_selector(bad_selector):
    from linuxlab.sync.outbox import OutboxRequeueError, requeue_outbox_message

    row = _make_dead_letter_row()
    before = OutboxMessage.objects.values().get(pk=row.pk)

    with pytest.raises(OutboxRequeueError):
        requeue_outbox_message(bad_selector)

    after = OutboxMessage.objects.values().get(pk=row.pk)
    assert after == before


def test_requeue_never_uses_the_local_primary_key_as_selector():
    from linuxlab.sync.outbox import OutboxRequeueError, requeue_outbox_message

    row = _make_dead_letter_row()

    # The local integer PK must never work as a substitute selector --
    # only event_id is the operator-facing identity.
    with pytest.raises(OutboxRequeueError):
        requeue_outbox_message(row.pk)


# --- capability / configuration preflight ------------------------------------


def test_requeue_denied_without_worker_capability(settings):
    from linuxlab.sync.outbox import OutboxRequeueError, requeue_outbox_message

    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = False
    row = _make_dead_letter_row()
    before = OutboxMessage.objects.values().get(pk=row.pk)

    with pytest.raises(OutboxRequeueError):
        requeue_outbox_message(row.event_id)

    after = OutboxMessage.objects.values().get(pk=row.pk)
    assert after == before


def test_requeue_profile_label_alone_does_not_grant_worker_capability(settings):
    """A Node LINUXLAB_PROFILE label with the capability flag left False
    must not itself authorize requeue -- the explicit capability flag is
    the sole authority (spec §5/§15), never the profile name."""
    from linuxlab.sync.outbox import OutboxRequeueError, requeue_outbox_message

    settings.LINUXLAB_PROFILE = "node"
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = False
    row = _make_dead_letter_row()

    with pytest.raises(OutboxRequeueError):
        requeue_outbox_message(row.event_id)


@pytest.mark.parametrize(
    "name,value",
    [
        ("LINUXLAB_SYNC_ENABLED", "CONFIG_SENTINEL_VALUE"),
        ("LINUXLAB_SYNC_BACKOFF_BASE_SECONDS", "CONFIG_SENTINEL_VALUE"),
        ("LINUXLAB_SYNC_BATCH_MAX_EVENTS", "CONFIG_SENTINEL_VALUE"),
        ("LINUXLAB_SYNC_LEASE_SECONDS", 0),
        ("LINUXLAB_NODE_ID", None),
    ],
)
def test_requeue_malformed_worker_config_makes_zero_writes(settings, name, value):
    from linuxlab.sync.outbox import OutboxRequeueError, requeue_outbox_message

    _configure_worker_settings(settings)
    setattr(settings, name, value)
    row = _make_dead_letter_row(claim_token=uuid.uuid4())
    before = OutboxMessage.objects.values().get(pk=row.pk)

    with pytest.raises(OutboxRequeueError):
        requeue_outbox_message(row.event_id)

    after = OutboxMessage.objects.values().get(pk=row.pk)
    assert after == before
    assert after["claim_token"] == before["claim_token"]
    assert after["attempts"] == before["attempts"]


def test_requeue_does_not_use_retry_purpose_config_as_a_substitute(settings):
    """Worker-purpose prerequisites (capability, enabled, credentials,
    batch/lease settings) are required, not merely retry-purpose bounds
    -- disabling sync entirely (retry-purpose config alone stays valid)
    must still reject requeue."""
    from linuxlab.sync.outbox import OutboxRequeueError, requeue_outbox_message

    _configure_worker_settings(settings)
    settings.LINUXLAB_SYNC_ENABLED = False
    row = _make_dead_letter_row()

    with pytest.raises(OutboxRequeueError):
        requeue_outbox_message(row.event_id)


# --- no recursion / no network / no new rows ---------------------------------


def test_requeue_never_publishes_a_domain_event(settings, monkeypatch):
    from linuxlab.sync.outbox import requeue_outbox_message

    _configure_worker_settings(settings)
    row = _make_dead_letter_row()

    def _boom(*args, **kwargs):
        raise AssertionError("publish_event must never be called by requeue")

    monkeypatch.setattr("linuxlab.core.events.publish_event", _boom)

    requeue_outbox_message(row.event_id)  # does not raise


def test_requeue_never_touches_the_network(settings, monkeypatch):
    from linuxlab.sync.outbox import requeue_outbox_message

    _configure_worker_settings(settings)
    row = _make_dead_letter_row()

    def _boom(*args, **kwargs):
        raise AssertionError("requeue must never open a network connection")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", _boom)

    requeue_outbox_message(row.event_id)  # does not raise


def test_requeue_creates_no_additional_outbox_row(settings):
    from linuxlab.sync.outbox import requeue_outbox_message

    _configure_worker_settings(settings)
    row = _make_dead_letter_row()
    before_count = OutboxMessage.objects.count()

    requeue_outbox_message(row.event_id)

    assert OutboxMessage.objects.count() == before_count


# --- transaction rollback on a mid-write failure -----------------------------


def test_requeue_rolls_back_completely_on_a_controlled_writer_failure(settings, monkeypatch):
    from linuxlab.sync import outbox as outbox_module

    _configure_worker_settings(settings)
    row = _make_dead_letter_row(claim_token=uuid.uuid4())
    before = OutboxMessage.objects.values().get(pk=row.pk)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated controlled-writer failure")

    monkeypatch.setattr(outbox_module, "_write_immutable_row_fields", _boom)

    with pytest.raises(RuntimeError):
        outbox_module.requeue_outbox_message(row.event_id)

    after = OutboxMessage.objects.values().get(pk=row.pk)
    assert after == before
    assert after["state"] == OUTBOX_DEAD_LETTER


# --- concurrency: exactly one of two concurrent requeues succeeds -----------


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
        except Exception as exc:  # noqa: BLE001 - captured across threads for assertion
            self.results[self.key] = exc
        finally:
            connection.close()


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_requeues_serialize_to_exactly_one_success(settings):
    from linuxlab.sync.outbox import OutboxRequeueError, requeue_outbox_message

    _configure_worker_settings(settings)
    row = _make_dead_letter_row()
    barrier = threading.Barrier(2)
    results = {}

    def _attempt():
        barrier.wait()
        return requeue_outbox_message(row.event_id)

    workers = [
        _RunOnOwnConnection(_attempt, results, "a"),
        _RunOnOwnConnection(_attempt, results, "b"),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    outcomes = [results["a"], results["b"]]
    successes = [o for o in outcomes if isinstance(o, OutboxMessage)]
    failures = [o for o in outcomes if isinstance(o, OutboxRequeueError)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert successes[0].state == OUTBOX_PENDING
    assert successes[0].attempts == 0
    assert successes[0].first_failure_at is None

    final = OutboxMessage.objects.get(pk=row.pk)
    assert final.state == OUTBOX_PENDING
    assert final.attempts == 0
    assert final.first_failure_at is None
    assert OutboxMessage.objects.filter(event_id=row.event_id).count() == 1


# --- safe error contract: no unsafe content ever leaks -----------------------


def test_requeue_error_messages_never_leak_sentinel_payload_or_config_values(settings):
    from linuxlab.sync.outbox import OutboxRequeueError, requeue_outbox_message

    _configure_worker_settings(settings)
    settings.LINUXLAB_SYNC_SECRET = "SECRET_SENTINEL_VALUE"
    row = _make_dead_letter_row(
        payload={"answer": "PAYLOAD_SENTINEL_VALUE"},
        rejection_reason="REASON_SENTINEL_VALUE",
    )

    # 1. Wrong-state rejection after a first successful requeue.
    requeue_outbox_message(row.event_id)
    with pytest.raises(OutboxRequeueError) as wrong_state:
        requeue_outbox_message(row.event_id)

    # 2. Malformed config rejection.
    settings.LINUXLAB_SYNC_ENABLED = "SECRET_SENTINEL_VALUE"
    other = _make_dead_letter_row(payload={"answer": "PAYLOAD_SENTINEL_VALUE"})
    with pytest.raises(OutboxRequeueError) as bad_config:
        requeue_outbox_message(other.event_id)

    for error in (wrong_state.value, bad_config.value):
        text = str(error) + repr(error)
        assert "SENTINEL" not in text
        assert "PAYLOAD_SENTINEL_VALUE" not in text
        assert "REASON_SENTINEL_VALUE" not in text
        assert "SECRET_SENTINEL_VALUE" not in text
