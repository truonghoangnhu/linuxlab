"""Task 14 end-to-end vertical slice and failure-recovery proofs.

Node and Central share one test database, so these tests use a small
in-process bridge transport that calls the REAL production
authenticate_node()/ingest_event() functions directly instead of an
HTTP server -- Task 6 already unit-tests HTTP wire serialization in
isolation; what these tests prove is that the real learning domain,
real enqueue wiring, real Outbox state machine, real Node
authentication, and real Central ingestion all integrate correctly
end to end.
"""

import uuid
from datetime import UTC, datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Account
from linuxlab.identity.services import ensure_learner
from linuxlab.learning.services import claim_activity_completion, start_attempt, submit_attempt
from linuxlab.sync.authentication import (
    NodeAuthenticationError,
    authenticate_node,
    issue_node_registration,
)
from linuxlab.sync.contracts import BatchAck, TransientTransportError
from linuxlab.sync.models import (
    INBOX_PROCESSED,
    OUTBOX_DEAD_LETTER,
    OUTBOX_DELIVERED,
    OUTBOX_PENDING,
    InboxMessage,
    InstalledActivityVersion,
    InstalledPublication,
    InstalledQuestionGrading,
    InstalledQuestionVersion,
    NodeRegistration,
    OutboxMessage,
    _insert_immutable_row,
)
from linuxlab.sync.outbox import flush_outbox
from linuxlab.sync.processing import process_inbox_event
from linuxlab.sync.transports import HttpSyncTransport

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _node_worker_configuration(settings):
    """Corrective B Task 8 Fix Round 1 (I3): the public flush_outbox()
    performs the full typed WORKER-purpose configuration preflight
    itself. Each test below still sets LINUXLAB_SYNC_ENABLED and its own
    real LINUXLAB_NODE_ID; this supplies the remaining worker
    prerequisites (capability, secret, Central URL)."""
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_SECRET = "the-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"


class _FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": 1, "max_score": 1}


class _FakeRegistry:
    def __init__(self, provider):
        self._provider = provider

    def get(self, type_key):
        assert type_key == self._provider.type_key
        return self._provider


def _make_learner():
    account = Account.objects.create_user(username=f"user-{uuid.uuid4()}", password="x")
    return ensure_learner(account)


def _make_published_activity_version():
    registry = _FakeRegistry(_FakeProvider())
    definition = ActivityDefinition.objects.create(name="Fake", type_key="fake")
    version = create_activity_version(definition, config={})
    published = publish_activity_version(version, registry)
    return published, registry


class _BridgeTransport:
    """Test-only bridge standing in for the network hop between Node's
    flush_outbox() and Central's authenticated ingestion endpoint --
    calls the REAL authenticate_node()/ingest_event() production
    functions in-process. `fail_first_n_sends` simulates transient
    failures (e.g. a lost response after Central already committed);
    `after_send` is an optional hook for observing/asserting durable
    Central state exactly once per real send."""

    def __init__(self, node_id, secret, *, fail_first_n_sends=0, after_send=None):
        self._node_id = node_id
        self._secret = secret
        self._remaining_failures = fail_first_n_sends
        self._after_send = after_send
        self.send_count = 0

    def send_batch(self, envelopes):
        from linuxlab.sync.ingestion import ingest_event

        self.send_count += 1
        try:
            node = authenticate_node(self._node_id, self._secret)
        except NodeAuthenticationError as exc:
            raise TransientTransportError("authentication failed") from exc

        results = tuple(ingest_event(node, envelope) for envelope in envelopes)

        if self._after_send is not None:
            self._after_send()

        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise TransientTransportError("simulated response loss")

        return BatchAck(results=results)


def _package_inventory_counts():
    return (
        InstalledPublication.objects.count(),
        InstalledActivityVersion.objects.count(),
        InstalledQuestionVersion.objects.count(),
        InstalledQuestionGrading.objects.count(),
    )


# --- full real vertical slice ------------------------------------------


def test_real_activity_completion_flows_end_to_end_to_delivered(settings):
    # Node registration provisioning is Central-only; enable the
    # capability just for this explicit issue call, then restore this
    # test's intended Node-worker profile immediately after.
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("Vertical Slice Node")
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(credential.node_id)

    published, _registry = _make_published_activity_version()
    learner = _make_learner()

    package_before = _package_inventory_counts()

    result = claim_activity_completion(learner, published)
    assert result is not None
    outbox = OutboxMessage.objects.get()
    assert outbox.state == OUTBOX_PENDING
    assert outbox.event_type == "activity.completed"

    transport = _BridgeTransport(credential.node_id, credential.secret)
    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.delivered == 1
    refreshed_outbox = OutboxMessage.objects.get(pk=outbox.pk)
    assert refreshed_outbox.state == OUTBOX_DELIVERED

    inbox = InboxMessage.objects.get(event_id=outbox.event_id)
    assert inbox.source_node.node_id == credential.node_id
    assert inbox.payload == outbox.payload
    assert inbox.aggregate_public_id == outbox.aggregate_public_id

    # Task 14 never mutates Central Attempt/Result/Progress, and never
    # touches the Task 12 package inventory.
    assert _package_inventory_counts() == package_before


# --- commit-then-ACK-lost replay --------------------------------------------


def test_commit_then_ack_lost_replay_yields_one_inbox_row_and_eventual_delivery(settings):
    # Node registration provisioning is Central-only; enable the
    # capability just for this explicit issue call, then restore this
    # test's intended Node-worker profile immediately after.
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("Replay Node")
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(credential.node_id)

    published, _registry = _make_published_activity_version()
    learner = _make_learner()
    claim_activity_completion(learner, published)
    outbox = OutboxMessage.objects.get()

    # First send: Central durably ingests the event, but the response is
    # "lost" before the Node ever sees the ACK.
    lossy_transport = _BridgeTransport(credential.node_id, credential.secret, fail_first_n_sends=1)
    first_summary = flush_outbox(lossy_transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)
    assert first_summary.released_transient == 1
    assert OutboxMessage.objects.get(pk=outbox.pk).state == OUTBOX_PENDING
    assert InboxMessage.objects.filter(event_id=outbox.event_id).count() == 1

    # Node retries the SAME event_id; Central recognizes the exact
    # duplicate and the Node finally sees DELIVERED.
    reliable_transport = _BridgeTransport(credential.node_id, credential.secret)
    second_summary = flush_outbox(
        reliable_transport,
        max_events=10,
        max_bytes=1_000_000,
        lease_seconds=60,
        now=timezone.now() + timedelta(hours=1),
    )

    assert second_summary.delivered == 1
    assert OutboxMessage.objects.get(pk=outbox.pk).state == OUTBOX_DELIVERED
    assert InboxMessage.objects.filter(event_id=outbox.event_id).count() == 1


# --- poison neighbor ---------------------------------------------------------


def test_poison_neighbor_does_not_block_good_events_in_the_same_batch(settings):
    # Node registration provisioning is Central-only; enable the
    # capability just for this explicit issue call, then restore this
    # test's intended Node-worker profile immediately after.
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("Poison Node")
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(credential.node_id)

    published, _registry = _make_published_activity_version()
    learner_1 = _make_learner()
    learner_2 = _make_learner()

    claim_activity_completion(learner_1, published)
    claim_activity_completion(learner_2, published)

    good_1, good_2 = OutboxMessage.objects.order_by("created_at")
    # Corrupt one Outbox row's stored payload directly to simulate a
    # poison event Central will permanently reject as INVALID_PAYLOAD --
    # bypassing the ordinary immutability guard is legitimate only for
    # constructing this test fixture.
    from django.db import models as django_models

    django_models.QuerySet.update(
        OutboxMessage.objects.filter(pk=good_2.pk), payload={"bogus": True}
    )

    transport = _BridgeTransport(credential.node_id, credential.secret)
    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.delivered == 1
    assert summary.dead_lettered == 1
    assert OutboxMessage.objects.get(pk=good_1.pk).state == OUTBOX_DELIVERED
    assert OutboxMessage.objects.get(pk=good_2.pk).state == OUTBOX_DEAD_LETTER
    assert InboxMessage.objects.filter(event_id=good_1.event_id).exists()
    assert not InboxMessage.objects.filter(event_id=good_2.event_id).exists()


# --- long-offline recovery ---------------------------------------------------


def test_repeated_transient_failures_never_delete_or_dead_letter_then_eventually_deliver(settings):
    # Node registration provisioning is Central-only; enable the
    # capability just for this explicit issue call, then restore this
    # test's intended Node-worker profile immediately after.
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("Offline Node")
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(credential.node_id)

    published, _registry = _make_published_activity_version()
    learner = _make_learner()
    claim_activity_completion(learner, published)
    outbox = OutboxMessage.objects.get()

    now = timezone.now()
    for _ in range(5):
        failing_transport = _BridgeTransport(
            credential.node_id, credential.secret, fail_first_n_sends=1
        )
        summary = flush_outbox(
            failing_transport, max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now
        )
        assert summary.released_transient == 1
        assert summary.dead_lettered == 0
        refreshed = OutboxMessage.objects.get(pk=outbox.pk)
        assert refreshed.state == OUTBOX_PENDING
        assert OutboxMessage.objects.filter(pk=outbox.pk).exists()
        # Advance past this row's backoff-computed next_attempt_at so
        # the NEXT iteration can reclaim it, exactly as a real Node
        # worker would after sleeping through its backoff.
        now = refreshed.next_attempt_at + timedelta(seconds=1)

    reliable_transport = _BridgeTransport(credential.node_id, credential.secret)
    final_summary = flush_outbox(
        reliable_transport,
        max_events=10,
        max_bytes=1_000_000,
        lease_seconds=60,
        now=now,
    )

    assert final_summary.delivered == 1
    refreshed = OutboxMessage.objects.get(pk=outbox.pk)
    assert refreshed.state == OUTBOX_DELIVERED
    assert refreshed.event_id == outbox.event_id
    assert refreshed.payload == outbox.payload


# --- processor failure after ACK ---------------------------------------------


def test_processor_failure_after_ack_never_requires_node_retransmission(settings):
    # Node registration provisioning is Central-only; enable the
    # capability just for this explicit issue call, then restore this
    # test's intended Node-worker profile immediately after.
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("Processor Node")
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(credential.node_id)

    published, _registry = _make_published_activity_version()
    learner = _make_learner()
    claim_activity_completion(learner, published)
    outbox = OutboxMessage.objects.get()

    transport = _BridgeTransport(credential.node_id, credential.secret)
    summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)
    assert summary.delivered == 1

    inbox = InboxMessage.objects.get(event_id=outbox.event_id)

    class _FailingProcessor:
        def process(self, inbox_message):
            raise ValueError("simulated processor failure")

    result = process_inbox_event(inbox, _FailingProcessor())

    assert result != INBOX_PROCESSED
    refreshed_outbox = OutboxMessage.objects.get(pk=outbox.pk)
    assert refreshed_outbox.state == OUTBOX_DELIVERED
    refreshed_inbox = InboxMessage.objects.get(pk=inbox.pk)
    assert refreshed_inbox.event_id == inbox.event_id
    assert refreshed_inbox.payload == inbox.payload


# --- no package / no Central->Node transport regression ---------------------


def test_full_cycle_never_touches_package_inventory_or_creates_reverse_transport(settings):
    # Node registration provisioning is Central-only; enable the
    # capability just for this explicit issue call, then restore this
    # test's intended Node-worker profile immediately after.
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("Regression Node")
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(credential.node_id)

    package_before = _package_inventory_counts()
    node_registration_count_before = NodeRegistration.objects.count()
    assert node_registration_count_before == 1  # only the explicit issue above

    published, _registry = _make_published_activity_version()
    learner = _make_learner()
    attempt = start_attempt(learner, published)
    submit_attempt(attempt, {}, _FakeRegistry(_FakeProvider()))

    transport = _BridgeTransport(credential.node_id, credential.secret)
    flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert _package_inventory_counts() == package_before
    # No new Node identity was silently created as a side effect of a
    # regular sync cycle -- only linuxlab.sync.authentication's explicit
    # controlled issue_node_registration() creates one.
    assert NodeRegistration.objects.count() == node_registration_count_before


# --- corrective Task 8 proofs ------------------------------------------------


def _fake_http_response(status, body_dict):
    import json

    response = MagicMock()
    response.status = status
    response.read.return_value = json.dumps(body_dict).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_413_split_and_singleton_dead_letter_through_real_http_transport(settings):
    """T14-D40 through the REAL shared serializer (serialize_sync_batch()),
    the REAL HttpSyncTransport HTTP status classification, and the REAL
    flush_outbox() split/singleton-dead-letter orchestration -- only the
    urllib.request.urlopen() network call itself is faked."""
    import json
    import urllib.error

    # Node registration provisioning is Central-only; enable the
    # capability just for this explicit issue call, then restore this
    # test's intended Node-worker profile immediately after.
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("413 Node")
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(credential.node_id)

    published, _registry = _make_published_activity_version()
    good_learner = _make_learner()
    oversized_learner = _make_learner()
    claim_activity_completion(good_learner, published)
    claim_activity_completion(oversized_learner, published)

    good_outbox, oversized_outbox = OutboxMessage.objects.order_by("created_at")
    oversized_event_id = str(oversized_outbox.event_id)

    transport = HttpSyncTransport(
        "https://central.example.org", credential.node_id, credential.secret
    )

    def _fake_urlopen(request, timeout=None):
        del timeout
        body = json.loads(request.data)
        events = body["events"]
        if len(events) > 1 or events[0]["event_id"] == oversized_event_id:
            raise urllib.error.HTTPError(
                url="https://central.example.org/api/v1/sync/events/",
                code=413,
                msg="too large",
                hdrs=None,
                fp=None,
            )
        return _fake_http_response(
            200, {"results": [{"event_id": events[0]["event_id"], "status": "accepted"}]}
        )

    with patch("linuxlab.sync.transports._TRANSPORT_OPENER.open", side_effect=_fake_urlopen):
        summary = flush_outbox(transport, max_events=10, max_bytes=1_000_000, lease_seconds=60)

    assert summary.claimed == 2
    assert summary.delivered == 1
    assert summary.dead_lettered == 1
    refreshed_good = OutboxMessage.objects.get(pk=good_outbox.pk)
    refreshed_oversized = OutboxMessage.objects.get(pk=oversized_outbox.pk)
    assert refreshed_good.state == OUTBOX_DELIVERED
    assert refreshed_oversized.state == OUTBOX_DEAD_LETTER
    assert refreshed_oversized.rejection_code == "EVENT_TOO_LARGE"


def test_ack_lost_replay_with_non_utc_offset_becomes_duplicate_not_conflict(settings):
    """A Node-encoded +07:00 occurred_at must be recognized as the exact
    same event as its UTC-normalized replica on retry -- through the
    REAL flush_outbox() -> Central authenticate_node()/ingest_event()
    path, not just a direct ingest_event() unit call."""
    # Node registration provisioning is Central-only; enable the
    # capability just for this explicit issue call, then restore this
    # test's intended Node-worker profile immediately after.
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("Offset Node")
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(credential.node_id)

    plus_seven = datetime(2026, 8, 31, 10, 0, 0, tzinfo=dt_timezone(timedelta(hours=7)))
    attempt_public_id = uuid.uuid4()
    now = timezone.now()
    outbox = _insert_immutable_row(
        OutboxMessage,
        event_id=uuid.uuid4(),
        domain_event=None,
        event_type="activity.completed",
        schema_version=1,
        source_node_id=credential.node_id,
        occurred_at=plus_seven,
        aggregate_type="attempt",
        aggregate_public_id=attempt_public_id,
        payload={
            "attempt_public_id": str(attempt_public_id),
            "learner_public_id": str(uuid.uuid4()),
            "activity_definition_public_id": str(uuid.uuid4()),
            "activity_version": 1,
            "score": 1,
            "max_score": 1,
        },
        state=OUTBOX_PENDING,
        attempts=0,
        next_attempt_at=now,
        claimed_at=None,
        lease_until=None,
        claim_token=None,
        delivered_at=None,
        rejection_code="",
        rejection_reason="",
    )

    lossy_transport = _BridgeTransport(credential.node_id, credential.secret, fail_first_n_sends=1)
    first_summary = flush_outbox(
        lossy_transport, max_events=10, max_bytes=1_000_000, lease_seconds=60, now=now
    )
    assert first_summary.released_transient == 1
    inbox = InboxMessage.objects.get(event_id=outbox.event_id)
    assert inbox.occurred_at == plus_seven.astimezone(UTC)

    later = now + timedelta(hours=1)
    reliable_transport = _BridgeTransport(credential.node_id, credential.secret)
    second_summary = flush_outbox(
        reliable_transport, max_events=10, max_bytes=1_000_000, lease_seconds=60, now=later
    )

    assert second_summary.delivered == 1
    assert OutboxMessage.objects.get(pk=outbox.pk).state == OUTBOX_DELIVERED
    assert InboxMessage.objects.filter(event_id=outbox.event_id).count() == 1
