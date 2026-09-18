"""Task 14 Node outbox sync worker (T14-D01, T14-D02, T14-D16, T14-D17),
plus Corrective B Task 9's explicit `requeue` operator adapter (approved
spec §15).

Usage:
    manage.py sync_events --once   # single flush cycle, for admin/testing
    manage.py sync_events          # continuous loop, for service-manager use
    manage.py sync_events requeue --event-id <EVENT_UUID>
                                    # explicit reset of one dead_letter row

No background thread runs inside the Django web process (T14-D02);
this is a separate, explicitly supervised process. `requeue` is mutually
exclusive with worker execution -- it dispatches to
linuxlab.sync.outbox.requeue_outbox_message() and returns immediately,
before any worker loop, signal-handler, or transport setup; it never
sends the event itself.
"""

import signal
import time
import uuid

from django.core.management.base import BaseCommand, CommandError

from linuxlab.sync.configuration import SyncConfigurationError, load_sync_configuration
from linuxlab.sync.outbox import OutboxRequeueError, flush_outbox, requeue_outbox_message
from linuxlab.sync.transports import HttpSyncTransport

_IDLE_SLEEP_SECONDS = 5
_AUTH_FAULT_SLEEP_SECONDS = 60
_REQUEUE_ACTION = "requeue"
_INVALID_EVENT_ID_MESSAGE = "invalid event identifier"


class Command(BaseCommand):
    help = "Flush this Node's sync Outbox to Central, once or continuously."

    _running = True

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            nargs="?",
            choices=[_REQUEUE_ACTION],
            default=None,
            help="Optional explicit operator action. 'requeue' resets one "
            "dead_letter Outbox row to pending; omit for ordinary worker use.",
        )
        parser.add_argument(
            "--once", action="store_true", help="Run a single flush cycle and exit."
        )
        parser.add_argument(
            "--event-id",
            default=None,
            help="Outbox event_id (UUID) to reset. Required with, and only with, "
            "the 'requeue' action.",
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

    def _handle_requeue(self, options):
        if options.get("once"):
            raise CommandError("requeue is mutually exclusive with --once")
        raw_event_id = options.get("event_id")
        # Semantic presence check: an omitted --event-id is None; a
        # SUPPLIED but empty "--event-id \"\"" is "" -- distinct cases
        # (Fix Round 1 I1). Only the former is "requeue requires
        # --event-id"; the latter is a supplied-but-invalid selector and
        # falls through to the same "invalid event identifier" outcome
        # as any other malformed UUID text.
        if raw_event_id is None:
            raise CommandError("requeue requires --event-id")
        try:
            event_id = uuid.UUID(raw_event_id)
        except (ValueError, AttributeError, TypeError):
            raise CommandError(_INVALID_EVENT_ID_MESSAGE) from None
        try:
            requeue_outbox_message(event_id)
        except OutboxRequeueError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"requeued {event_id}")

    def handle(self, *args, **options):
        del args
        if options.get("action") == _REQUEUE_ACTION:
            self._handle_requeue(options)
            return
        # Semantic presence check (Fix Round 1 I1): options.get("event_id")
        # is None only when --event-id was never given at all -- a
        # SUPPLIED but empty "--event-id \"\"" is "", which is falsy but
        # NOT absent, and must still be rejected here rather than
        # silently falling through into ordinary worker execution.
        if options.get("event_id") is not None:
            raise CommandError("--event-id requires the requeue action")

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
