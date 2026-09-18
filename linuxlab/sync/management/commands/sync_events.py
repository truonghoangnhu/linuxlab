"""Task 14 Node outbox sync worker (T14-D01, T14-D02, T14-D16, T14-D17).

Usage:
    manage.py sync_events --once   # single flush cycle, for admin/testing
    manage.py sync_events          # continuous loop, for service-manager use

No background thread runs inside the Django web process (T14-D02);
this is a separate, explicitly supervised process.
"""

import signal
import time

from django.core.management.base import BaseCommand, CommandError

from linuxlab.sync.configuration import SyncConfigurationError, load_sync_configuration
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

    def _load_configuration(self):
        try:
            return load_sync_configuration(purpose="worker")
        except SyncConfigurationError as exc:
            raise CommandError(str(exc)) from None

    def _build_transport(self, configuration=None):
        configuration = configuration if configuration is not None else self._load_configuration()
        credentials = configuration.credentials
        return HttpSyncTransport(
            credentials.central_url,
            credentials.node_id,
            credentials.secret,
            allow_insecure_http=credentials.allow_insecure_http,
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
            f"auth_fault={summary.auth_fault} protocol_fault={summary.protocol_fault}"
        )
        if summary.protocol_fault:
            # Corrective B Task 8: a deterministic 400 (PERMANENT_REJECTION)
            # means Central rejected the request itself as malformed --
            # every claimed event in that batch dead-letters immediately
            # and will not resolve on its own, so surface it distinctly
            # rather than letting it blend into ordinary transient-failure
            # log noise.
            self.stderr.write(
                self.style.WARNING(
                    "sync_events: Central rejected a request as malformed (HTTP 400); "
                    "the claimed event(s) in that batch were dead-lettered and this "
                    "will not resolve on its own -- investigate the Node/Central "
                    "protocol version mismatch or a local batching defect"
                )
            )

    def handle(self, *args, **options):
        del args
        configuration = self._load_configuration()
        transport = self._build_transport(configuration)
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_stop_signal)
        signal.signal(signal.SIGINT, self._handle_stop_signal)

        while self._running:
            summary = flush_outbox(
                transport,
                max_events=configuration.batch_max_events,
                max_bytes=configuration.batch_max_bytes,
                lease_seconds=configuration.lease_seconds,
            )
            self._report(summary)

            if options.get("once"):
                return

            if summary.auth_fault or summary.protocol_fault:
                time.sleep(_AUTH_FAULT_SLEEP_SECONDS)
            elif summary.claimed == 0:
                time.sleep(_IDLE_SLEEP_SECONDS)
