"""Task 14 Node outbox sync worker (T14-D01, T14-D02, T14-D16, T14-D17).

Usage:
    manage.py sync_events --once   # single flush cycle, for admin/testing
    manage.py sync_events          # continuous loop, for service-manager use

No background thread runs inside the Django web process (T14-D02);
this is a separate, explicitly supervised process.
"""

import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from linuxlab.sync.outbox import flush_outbox
from linuxlab.sync.transports import HttpSyncTransport

_IDLE_SLEEP_SECONDS = 5
_AUTH_FAULT_SLEEP_SECONDS = 60


class Command(BaseCommand):
    help = "Flush this Node's sync Outbox to Central, once or continuously."

    _running = True

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true", help="Run a single flush cycle and exit."
        )

    def _build_transport(self):
        if not settings.LINUXLAB_SYNC_ENABLED:
            raise CommandError("LINUXLAB_SYNC_ENABLED is false; sync_events refuses to run")
        if not settings.LINUXLAB_NODE_ID:
            raise CommandError("LINUXLAB_NODE_ID is not configured")
        if not settings.LINUXLAB_SYNC_SECRET:
            raise CommandError("LINUXLAB_SYNC_SECRET is not configured")
        if not settings.LINUXLAB_CENTRAL_URL:
            raise CommandError("LINUXLAB_CENTRAL_URL is not configured")
        return HttpSyncTransport(
            settings.LINUXLAB_CENTRAL_URL,
            settings.LINUXLAB_NODE_ID,
            settings.LINUXLAB_SYNC_SECRET,
            allow_insecure_http=settings.LINUXLAB_SYNC_ALLOW_INSECURE_HTTP,
        )

    def _handle_stop_signal(self, signum, frame):
        del signum, frame
        self._running = False

    def _report(self, summary):
        self.stdout.write(
            f"claimed={summary.claimed} delivered={summary.delivered} "
            f"dead_lettered={summary.dead_lettered} "
            f"released_transient={summary.released_transient} "
            f"released_auth_failure={summary.released_auth_failure} "
            f"auth_fault={summary.auth_fault}"
        )

    def handle(self, *args, **options):
        del args
        transport = self._build_transport()
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_stop_signal)
        signal.signal(signal.SIGINT, self._handle_stop_signal)

        while self._running:
            summary = flush_outbox(
                transport,
                max_events=settings.LINUXLAB_SYNC_BATCH_MAX_EVENTS,
                max_bytes=settings.LINUXLAB_SYNC_BATCH_MAX_BYTES,
                lease_seconds=settings.LINUXLAB_SYNC_LEASE_SECONDS,
            )
            self._report(summary)

            if options.get("once"):
                return

            if summary.auth_fault:
                time.sleep(_AUTH_FAULT_SLEEP_SECONDS)
            elif summary.claimed == 0:
                time.sleep(_IDLE_SLEEP_SECONDS)
