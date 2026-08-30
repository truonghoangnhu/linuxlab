"""Task 14 `sync_events` management command (T14-D02): --once for
deterministic testing/admin, a continuous supervised loop otherwise, no
background thread inside the Django web process.
"""

import signal
import uuid
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from linuxlab.sync.management.commands.sync_events import Command
from linuxlab.sync.outbox import FlushSummary

pytestmark = pytest.mark.django_db


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
