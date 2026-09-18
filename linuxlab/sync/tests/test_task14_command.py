"""Task 14 `sync_events` management command (T14-D02): --once for
deterministic testing/admin, a continuous supervised loop otherwise, no
background thread inside the Django web process.
"""

import io
import signal
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from linuxlab.sync.management.commands.sync_events import Command
from linuxlab.sync.models import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_PENDING,
    OutboxMessage,
    _insert_immutable_row,
)
from linuxlab.sync.outbox import FlushSummary

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _node_sync_worker_authority(settings):
    """This file exercises the sync_events worker's own (non-profile)
    failure modes; profile gating itself is proven independently in
    test_task14_profiles.py."""
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True


def _configure_sync_settings(settings):
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(uuid.uuid4())
    settings.LINUXLAB_SYNC_SECRET = "the-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"


def test_once_runs_exactly_one_flush_cycle_and_returns(settings):
    _configure_sync_settings(settings)

    with (
        patch(
            "linuxlab.sync.management.commands.sync_events.flush_outbox",
            return_value=FlushSummary(),
        ) as flush,
        patch(
            "linuxlab.sync.management.commands.sync_events.Command._build_transport",
            return_value=object(),
        ),
    ):
        call_command("sync_events", "--once")

    assert flush.call_count == 1


def test_refuses_to_run_when_sync_disabled(settings):
    settings.LINUXLAB_SYNC_ENABLED = False

    with pytest.raises(CommandError):
        call_command("sync_events", "--once")


def test_refuses_to_run_without_node_id(settings):
    _configure_sync_settings(settings)
    settings.LINUXLAB_NODE_ID = None

    with pytest.raises(CommandError):
        call_command("sync_events", "--once")


def test_refuses_to_run_without_secret(settings):
    _configure_sync_settings(settings)
    settings.LINUXLAB_SYNC_SECRET = None

    with pytest.raises(CommandError):
        call_command("sync_events", "--once")


def test_refuses_to_run_without_central_url(settings):
    _configure_sync_settings(settings)
    settings.LINUXLAB_CENTRAL_URL = None

    with pytest.raises(CommandError):
        call_command("sync_events", "--once")


def test_stop_signal_handler_ends_the_continuous_loop():
    command = Command()
    command._running = True

    command._handle_stop_signal(signal.SIGTERM, None)

    assert command._running is False


def test_continuous_loop_flushes_repeatedly_until_signaled_to_stop(settings):
    _configure_sync_settings(settings)
    command = Command()

    call_count = 0

    def _fake_flush(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            command._running = False
        return FlushSummary(claimed=1, delivered=1)

    with (
        patch(
            "linuxlab.sync.management.commands.sync_events.flush_outbox",
            side_effect=_fake_flush,
        ),
        patch.object(command, "_build_transport", return_value=object()),
        patch("linuxlab.sync.management.commands.sync_events.time.sleep"),
    ):
        command.handle(once=False)

    assert call_count == 3


def test_auth_fault_pauses_longer_than_idle(settings):
    _configure_sync_settings(settings)
    command = Command()

    call_count = 0

    def _fake_flush(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            command._running = False
        return FlushSummary(claimed=1, released_auth_failure=1, auth_fault=True)

    with (
        patch(
            "linuxlab.sync.management.commands.sync_events.flush_outbox",
            side_effect=_fake_flush,
        ),
        patch.object(command, "_build_transport", return_value=object()),
        patch("linuxlab.sync.management.commands.sync_events.time.sleep") as sleep_mock,
    ):
        command.handle(once=False)

    sleep_mock.assert_called_with(60)


def test_protocol_fault_pauses_longer_than_idle_and_warns_operator(settings):
    _configure_sync_settings(settings)
    command = Command(stderr=io.StringIO())

    call_count = 0

    def _fake_flush(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            command._running = False
        return FlushSummary(claimed=1, released_transient=1, protocol_fault=True)

    with (
        patch(
            "linuxlab.sync.management.commands.sync_events.flush_outbox",
            side_effect=_fake_flush,
        ),
        patch.object(command, "_build_transport", return_value=object()),
        patch("linuxlab.sync.management.commands.sync_events.time.sleep") as sleep_mock,
    ):
        command.handle(once=False)

    sleep_mock.assert_called_with(60)
    assert "malformed" in command.stderr.getvalue().lower()


@pytest.mark.parametrize(
    "name,value",
    [
        ("LINUXLAB_SYNC_ENABLED", "SECRET_SENTINEL"),
        ("LINUXLAB_SYNC_BACKOFF_BASE_SECONDS", "SECRET_SENTINEL"),
        ("LINUXLAB_SYNC_BATCH_MAX_EVENTS", "SECRET_SENTINEL"),
        ("LINUXLAB_SYNC_LEASE_SECONDS", 0),
        ("LINUXLAB_SYNC_ALLOW_INSECURE_HTTP", "SECRET_SENTINEL"),
    ],
)
def test_invalid_worker_configuration_rejected_before_transport(settings, name, value, caplog):
    from linuxlab.sync.models import OutboxMessage

    _configure_sync_settings(settings)
    setattr(settings, name, value)
    before = list(OutboxMessage.objects.values())
    out, err = io.StringIO(), io.StringIO()
    with (
        patch("linuxlab.sync.management.commands.sync_events.HttpSyncTransport") as transport,
        patch("linuxlab.sync.management.commands.sync_events.flush_outbox") as flush,
        pytest.raises(CommandError) as caught,
    ):
        call_command("sync_events", "--once", stdout=out, stderr=err)
    transport.assert_not_called()
    flush.assert_not_called()
    assert list(OutboxMessage.objects.values()) == before
    assert (
        "SECRET_SENTINEL" not in str(caught.value) + out.getvalue() + err.getvalue() + caplog.text
    )


# --- Corrective B Task 9: `sync_events requeue --event-id <UUID>` -----------


def _make_dead_letter_outbox(**overrides):
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
        "state": OUTBOX_DEAD_LETTER,
        "attempts": 2,
        "first_failure_at": now - timedelta(hours=1),
        "last_failure_category": "PERMANENT_REJECTION",
        "rejection_code": "IDEMPOTENCY_CONFLICT",
        "rejection_reason": "rejected by Central as a permanent failure",
        "next_attempt_at": now - timedelta(minutes=30),
        "claimed_at": None,
        "lease_until": None,
        "delivered_at": None,
    }
    fields.update(overrides)
    return _insert_immutable_row(OutboxMessage, **fields)


def test_requeue_cli_success_prints_exact_confirmation(settings):
    _configure_sync_settings(settings)
    row = _make_dead_letter_outbox()
    out = io.StringIO()

    call_command("sync_events", "requeue", "--event-id", str(row.event_id), stdout=out)

    assert out.getvalue() == f"requeued {row.event_id}\n"
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING
    assert refreshed.attempts == 0


def test_requeue_cli_dispatches_before_worker_setup_no_transport_no_network(settings):
    _configure_sync_settings(settings)
    row = _make_dead_letter_outbox()

    with (
        patch("linuxlab.sync.management.commands.sync_events.HttpSyncTransport") as transport,
        patch("linuxlab.sync.management.commands.sync_events.flush_outbox") as flush,
        patch("linuxlab.sync.management.commands.sync_events.signal.signal") as signal_setup,
    ):
        call_command("sync_events", "requeue", "--event-id", str(row.event_id))

    transport.assert_not_called()
    flush.assert_not_called()
    signal_setup.assert_not_called()


def test_requeue_cli_missing_event_id_is_rejected(settings):
    _configure_sync_settings(settings)

    with pytest.raises(CommandError):
        call_command("sync_events", "requeue")


def test_requeue_cli_rejects_once_combined_with_requeue(settings):
    _configure_sync_settings(settings)
    row = _make_dead_letter_outbox()

    with pytest.raises(CommandError):
        call_command("sync_events", "requeue", "--once", "--event-id", str(row.event_id))

    # Rejected before any mutation.
    assert OutboxMessage.objects.get(pk=row.pk).state == OUTBOX_DEAD_LETTER


def test_requeue_cli_event_id_without_action_is_rejected(settings):
    _configure_sync_settings(settings)
    row = _make_dead_letter_outbox()

    with pytest.raises(CommandError):
        call_command("sync_events", "--event-id", str(row.event_id))

    assert OutboxMessage.objects.get(pk=row.pk).state == OUTBOX_DEAD_LETTER


def test_requeue_cli_rejects_unknown_positional_action(settings):
    _configure_sync_settings(settings)

    with pytest.raises(CommandError):
        call_command("sync_events", "diagnose")


def test_requeue_cli_rejects_invalid_uuid_text(settings, caplog):
    _configure_sync_settings(settings)
    out, err = io.StringIO(), io.StringIO()

    with pytest.raises(CommandError) as caught:
        call_command(
            "sync_events", "requeue", "--event-id", "not-a-uuid<script>", stdout=out, stderr=err
        )

    text = str(caught.value) + out.getvalue() + err.getvalue() + caplog.text
    assert "not-a-uuid" not in text
    assert "<script>" not in text


def test_requeue_cli_rejects_unknown_event_id_safely(settings, caplog):
    _configure_sync_settings(settings)
    out, err = io.StringIO(), io.StringIO()
    unknown_id = uuid.uuid4()

    with pytest.raises(CommandError):
        call_command(
            "sync_events", "requeue", "--event-id", str(unknown_id), stdout=out, stderr=err
        )

    assert OutboxMessage.objects.count() == 0


def test_requeue_cli_output_never_leaks_payload_or_rejection_reason(settings):
    _configure_sync_settings(settings)
    row = _make_dead_letter_outbox(
        payload={"answer": "PAYLOAD_SENTINEL_VALUE"},
        rejection_reason="REASON_SENTINEL_VALUE",
    )
    out, err = io.StringIO(), io.StringIO()

    call_command("sync_events", "requeue", "--event-id", str(row.event_id), stdout=out, stderr=err)

    combined = out.getvalue() + err.getvalue()
    assert "PAYLOAD_SENTINEL_VALUE" not in combined
    assert "REASON_SENTINEL_VALUE" not in combined
    assert combined == f"requeued {row.event_id}\n"


def test_existing_worker_forms_are_unaffected_by_the_requeue_action(settings):
    """`sync_events` and `sync_events --once` must behave exactly as
    before -- the new optional positional `action` defaults to None and
    changes nothing about ordinary worker dispatch."""
    _configure_sync_settings(settings)

    with (
        patch(
            "linuxlab.sync.management.commands.sync_events.flush_outbox",
            return_value=FlushSummary(),
        ) as flush,
        patch(
            "linuxlab.sync.management.commands.sync_events.Command._build_transport",
            return_value=object(),
        ),
    ):
        call_command("sync_events", "--once")

    assert flush.call_count == 1
