"""Task 14 Central processor boundary (T14-D12): RECEIVED -> PROCESSED |
FAILED, never touching Attempt/Result/Progress or Node ACK/delivery
semantics.
"""

import uuid

import pytest
from django.test import override_settings
from django.utils import timezone

from linuxlab.sync.authentication import issue_node_registration
from linuxlab.sync.models import INBOX_FAILED, INBOX_PROCESSED, INBOX_RECEIVED, InboxMessage
from linuxlab.sync.processing import process_inbox_event

pytestmark = pytest.mark.django_db


def _make_inbox_message(**overrides):
    # Node registration provisioning is Central-only; enable the
    # capability just for this explicit issue call and let it restore
    # immediately after (never enabled globally in conftest).
    with override_settings(LINUXLAB_CAN_RECEIVE_SYNC_EVENTS=True):
        credential = issue_node_registration("Test Node")
    from linuxlab.sync.models import NodeRegistration, _insert_immutable_row

    node = NodeRegistration.objects.get(node_id=credential.node_id)
    now = timezone.now()
    fields = {
        "event_id": uuid.uuid4(),
        "source_node": node,
        "event_type": "activity.completed",
        "schema_version": 1,
        "occurred_at": now,
        "aggregate_type": "attempt",
        "aggregate_public_id": uuid.uuid4(),
        "payload": {"score": 1, "max_score": 1},
    }
    fields.update(overrides)
    return _insert_immutable_row(InboxMessage, **fields)


class _SucceedingProcessor:
    def __init__(self):
        self.seen = []

    def process(self, inbox_message):
        self.seen.append(inbox_message.pk)


class _FailingProcessor:
    def process(self, inbox_message):
        raise ValueError("simulated processor failure")


def test_accepted_inbox_message_begins_received():
    row = _make_inbox_message()

    assert row.processing_state == INBOX_RECEIVED


def test_successful_processor_marks_processed_with_timestamp():
    row = _make_inbox_message()
    now = timezone.now()
    processor = _SucceedingProcessor()

    result = process_inbox_event(row, processor, now=now)

    assert result == INBOX_PROCESSED
    refreshed = InboxMessage.objects.get(pk=row.pk)
    assert refreshed.processing_state == INBOX_PROCESSED
    assert refreshed.processed_at == now
    assert processor.seen == [row.pk]


def test_failing_processor_marks_failed_with_safe_error_metadata():
    row = _make_inbox_message()

    result = process_inbox_event(row, _FailingProcessor())

    assert result == INBOX_FAILED
    refreshed = InboxMessage.objects.get(pk=row.pk)
    assert refreshed.processing_state == INBOX_FAILED
    assert "simulated processor failure" in refreshed.processing_error
    assert refreshed.processed_at is None


def test_processing_failure_leaves_event_envelope_unchanged():
    row = _make_inbox_message()

    process_inbox_event(row, _FailingProcessor())

    refreshed = InboxMessage.objects.get(pk=row.pk)
    assert refreshed.event_id == row.event_id
    assert refreshed.event_type == row.event_type
    assert refreshed.payload == row.payload
    assert refreshed.aggregate_public_id == row.aggregate_public_id


def test_processing_does_not_mutate_learning_domain_models():
    from linuxlab.learning.models import Attempt, Result

    row = _make_inbox_message()
    before_attempts = Attempt.objects.count()
    before_results = Result.objects.count()

    process_inbox_event(row, _SucceedingProcessor())
    process_inbox_event(row, _FailingProcessor())

    assert Attempt.objects.count() == before_attempts
    assert Result.objects.count() == before_results
