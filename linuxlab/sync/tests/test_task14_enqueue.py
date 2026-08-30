"""Task 14 explicit transactional enqueue and the activity.completed
vertical slice (T14-D10, D11, D20, D21, D32, D38).

Both real production publish_event("activity.completed", ...) call sites
(linuxlab.learning.services.claim_activity_completion and submit_attempt)
are wired to call enqueue_sync_event() explicitly, in the same
transaction, immediately after publish_event()."""

import uuid

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.core.events import publish_event
from linuxlab.identity.services import ensure_learner
from linuxlab.learning.services import claim_activity_completion, submit_attempt
from linuxlab.sync.enqueue import SyncConfigurationError, enqueue_sync_event
from linuxlab.sync.models import OutboxMessage

pytestmark = pytest.mark.django_db


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
    from linuxlab.identity.models import Account

    account = Account.objects.create_user(username=f"user-{uuid.uuid4()}", password="x")
    return ensure_learner(account)


def _make_published_activity_version():
    registry = _FakeRegistry(_FakeProvider())
    definition = ActivityDefinition.objects.create(name="Fake", type_key="fake")
    version = create_activity_version(definition, config={})
    published = publish_activity_version(version, registry)
    return published, registry


# --- enqueue_sync_event() unit behavior -------------------------------------


def test_enqueue_returns_none_when_sync_disabled():
    domain_event = publish_event("activity.completed", {"x": 1}, uuid.uuid4())

    result = enqueue_sync_event(domain_event, node_id=uuid.uuid4(), sync_enabled=False)

    assert result is None
    assert OutboxMessage.objects.count() == 0


def test_enqueue_returns_none_for_unregistered_event_type():
    domain_event = publish_event("some.unregistered.event", {"x": 1}, uuid.uuid4())

    result = enqueue_sync_event(domain_event, node_id=uuid.uuid4(), sync_enabled=True)

    assert result is None
    assert OutboxMessage.objects.count() == 0


def test_enqueue_raises_when_sync_enabled_but_node_id_missing():
    domain_event = publish_event(
        "activity.completed",
        {
            "attempt_public_id": str(uuid.uuid4()),
            "learner_public_id": str(uuid.uuid4()),
            "activity_definition_public_id": str(uuid.uuid4()),
            "activity_version": 1,
            "score": 1,
            "max_score": 1,
        },
        uuid.uuid4(),
    )

    with pytest.raises(SyncConfigurationError):
        enqueue_sync_event(domain_event, node_id=None, sync_enabled=True)

    assert OutboxMessage.objects.count() == 0


def test_enqueue_creates_one_immutable_outbox_row_for_registered_event():
    node_id = uuid.uuid4()
    attempt_public_id = uuid.uuid4()
    domain_event = publish_event(
        "activity.completed",
        {
            "attempt_public_id": str(attempt_public_id),
            "learner_public_id": str(uuid.uuid4()),
            "activity_definition_public_id": str(uuid.uuid4()),
            "activity_version": 1,
            "score": 1,
            "max_score": 1,
        },
        attempt_public_id,
    )

    outbox = enqueue_sync_event(domain_event, node_id=node_id, sync_enabled=True)

    assert isinstance(outbox, OutboxMessage)
    assert outbox.event_id == domain_event.event_id
    assert outbox.event_type == "activity.completed"
    assert outbox.schema_version == 1
    assert outbox.source_node_id == node_id
    assert outbox.aggregate_type == "attempt"
    assert outbox.aggregate_public_id == attempt_public_id
    assert set(outbox.payload) == {
        "attempt_public_id",
        "learner_public_id",
        "activity_definition_public_id",
        "activity_version",
        "score",
        "max_score",
    }
    assert OutboxMessage.objects.count() == 1


def test_enqueue_payload_never_includes_extra_fields_like_guest_claim_source():
    """claim_activity_completion()'s DomainEvent payload includes an
    extra "source" field the submit_attempt() one does not -- the wire
    payload contract must select only the shared, minimal field set."""
    node_id = uuid.uuid4()
    attempt_public_id = uuid.uuid4()
    domain_event = publish_event(
        "activity.completed",
        {
            "attempt_public_id": str(attempt_public_id),
            "learner_public_id": str(uuid.uuid4()),
            "activity_definition_public_id": str(uuid.uuid4()),
            "activity_version": 1,
            "score": 1,
            "max_score": 1,
            "source": "guest_claim",
        },
        attempt_public_id,
    )

    outbox = enqueue_sync_event(domain_event, node_id=node_id, sync_enabled=True)

    assert "source" not in outbox.payload
    assert "answer_key" not in outbox.payload
    assert "scoring" not in outbox.payload


def test_enqueue_second_call_for_same_domain_event_raises_integrity_error():
    node_id = uuid.uuid4()
    domain_event = publish_event(
        "activity.completed",
        {
            "attempt_public_id": str(uuid.uuid4()),
            "learner_public_id": str(uuid.uuid4()),
            "activity_definition_public_id": str(uuid.uuid4()),
            "activity_version": 1,
            "score": 1,
            "max_score": 1,
        },
        uuid.uuid4(),
    )
    enqueue_sync_event(domain_event, node_id=node_id, sync_enabled=True)

    with pytest.raises(IntegrityError), transaction.atomic():
        enqueue_sync_event(domain_event, node_id=node_id, sync_enabled=True)


# --- real production call-site wiring: claim_activity_completion() ---------


@override_settings(LINUXLAB_SYNC_ENABLED=False)
def test_sync_disabled_keeps_domain_event_local_only_for_guest_claim():
    published, _registry = _make_published_activity_version()
    learner = _make_learner()

    result = claim_activity_completion(learner, published)

    assert result is not None
    assert OutboxMessage.objects.count() == 0


@override_settings(LINUXLAB_SYNC_ENABLED=True, LINUXLAB_NODE_ID=None)
def test_sync_enabled_without_node_id_fails_loudly_for_guest_claim():
    published, _registry = _make_published_activity_version()
    learner = _make_learner()

    with pytest.raises(SyncConfigurationError):
        claim_activity_completion(learner, published)


def test_sync_enabled_creates_exactly_one_outbox_row_for_guest_claim(settings):
    node_id = uuid.uuid4()
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(node_id)
    published, _registry = _make_published_activity_version()
    learner = _make_learner()

    result = claim_activity_completion(learner, published)

    assert result is not None
    assert OutboxMessage.objects.count() == 1
    outbox = OutboxMessage.objects.get()
    assert outbox.event_type == "activity.completed"
    assert str(outbox.source_node_id) == str(node_id)


# --- real production call-site wiring: submit_attempt() --------------------


def test_sync_enabled_creates_exactly_one_outbox_row_for_submit_attempt(settings):
    node_id = uuid.uuid4()
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(node_id)
    published, registry = _make_published_activity_version()
    learner = _make_learner()
    from linuxlab.learning.services import start_attempt

    attempt = start_attempt(learner, published)

    result = submit_attempt(attempt, {}, registry)

    assert result is not None
    assert OutboxMessage.objects.count() == 1
    outbox = OutboxMessage.objects.get()
    assert outbox.aggregate_public_id == attempt.public_id


def test_business_rollback_rolls_back_domain_event_and_outbox_together(settings):
    """publish_event() (creating a DomainEvent row) and the Result row it
    follows both already committed to the transaction's pending work by
    the time enqueue_sync_event() runs; if enqueue_sync_event() itself
    fails, the WHOLE business transaction -- Result, DomainEvent, and any
    partial Outbox work -- must roll back together (T14-D10)."""
    node_id = uuid.uuid4()
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(node_id)
    published, registry = _make_published_activity_version()
    learner = _make_learner()
    from unittest.mock import patch

    from linuxlab.core.models import DomainEvent
    from linuxlab.learning.models import Result
    from linuxlab.learning.services import start_attempt

    attempt = start_attempt(learner, published)

    before_domain_events = DomainEvent.objects.count()
    before_results = Result.objects.count()

    class _Boom(Exception):
        pass

    with (
        patch(
            "linuxlab.learning.services.enqueue_sync_event",
            side_effect=_Boom("simulated enqueue failure"),
        ),
        pytest.raises(_Boom),
    ):
        submit_attempt(attempt, {}, registry)

    assert DomainEvent.objects.count() == before_domain_events
    assert Result.objects.count() == before_results
    assert OutboxMessage.objects.count() == 0
