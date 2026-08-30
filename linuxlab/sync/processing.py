"""Task 14 Central processor boundary (T14-D12).

process_inbox_event() runs a caller-supplied SyncEventProcessor against
one already-accepted InboxMessage and records ONLY processing metadata
(processing_state, processed_at, processing_error) -- it never mutates
Attempt, Result, or Progress, and has no effect whatsoever on Node
ACK/delivery semantics (those already completed, durably, at ingest
time in linuxlab.sync.ingestion). Task 14 defines no processor that
writes Central learning aggregates; that is out of this task's scope.
"""

from typing import Protocol

from django.utils import timezone

from .models import INBOX_FAILED, INBOX_PROCESSED, InboxMessage, _write_immutable_row_fields

_INBOX_PROCESSING_MUTABLE_FIELDS = frozenset(
    {"processing_state", "processed_at", "processing_error"}
)


class SyncEventProcessor(Protocol):
    """Caller-supplied processing logic for one accepted InboxMessage.
    Raise any exception to signal failure; process_inbox_event() catches
    it and records processing_state=FAILED with a safe string summary,
    never letting a processor's failure propagate into the ingestion
    boundary or affect Node ACK/delivery semantics."""

    def process(self, inbox_message: InboxMessage) -> None: ...


def process_inbox_event(inbox_message, processor, now=None):
    """Run `processor` against `inbox_message`, then write exactly the
    processing-metadata outcome -- RECEIVED -> PROCESSED on success,
    RECEIVED -> FAILED on any exception. Returns the resulting
    processing_state. The Inbox event envelope itself is never touched
    here (only linuxlab.sync.ingestion's insert can set those fields)."""
    now = now or timezone.now()
    try:
        processor.process(inbox_message)
    except Exception as exc:  # noqa: BLE001 -- this boundary must isolate
        # any caller-supplied processor's failure so it can never crash
        # or roll back the durable ingestion/ACK path that already
        # completed before processing ever runs.
        _write_immutable_row_fields(
            InboxMessage,
            inbox_message.pk,
            _INBOX_PROCESSING_MUTABLE_FIELDS,
            processing_state=INBOX_FAILED,
            processing_error=str(exc),
        )
        return INBOX_FAILED

    _write_immutable_row_fields(
        InboxMessage,
        inbox_message.pk,
        _INBOX_PROCESSING_MUTABLE_FIELDS,
        processing_state=INBOX_PROCESSED,
        processed_at=now,
    )
    return INBOX_PROCESSED
