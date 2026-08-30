"""Task 14 corrective: Central/Node deployment-profile authority
(independent review finding #1). Central accepts inbound sync
ingestion and provisions Node credentials; Node runs the outbound sync
worker -- neither profile may perform the other's half of the
protocol, gated by one fail-closed capability flag each
(`linuxlab.sync.capabilities`), reused identically by the HTTP adapter
and both management commands.
"""

import json
import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from linuxlab.sync.authentication import issue_node_registration
from linuxlab.sync.contracts import ACCEPTED, canonical_event_dict
from linuxlab.sync.contracts import WireEventEnvelope as _Envelope
from linuxlab.sync.management.commands.sync_events import Command as SyncEventsCommand
from linuxlab.sync.models import InboxMessage

pytestmark = pytest.mark.django_db

_URL = "/api/v1/sync/events/"


def _make_envelope(**overrides):
    fields = {
        "event_id": uuid.uuid4(),
        "event_type": "activity.completed",
        "occurred_at": timezone.now(),
        "source_node_id": uuid.uuid4(),
        "aggregate_type": "attempt",
        "aggregate_public_id": uuid.uuid4(),
        "schema_version": 1,
        "payload": {
            "attempt_public_id": str(uuid.uuid4()),
            "learner_public_id": str(uuid.uuid4()),
            "activity_definition_public_id": str(uuid.uuid4()),
            "activity_version": 1,
            "score": 1,
            "max_score": 1,
        },
    }
    fields.update(overrides)
    return _Envelope(**fields)


def _post_events(client, node_id, secret, envelopes):
    body = json.dumps({"events": [canonical_event_dict(e) for e in envelopes]})
    return client.post(
        _URL,
        data=body,
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {secret}",
        HTTP_X_LINUXLAB_NODE_ID=str(node_id),
    )


def _node_like(settings):
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True


def _central_like(settings):
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = False


# --- Node profile: ingestion/provisioning rejected, worker available -------


def test_node_profile_rejects_ingestion_with_no_durable_inbox_row(client, settings):
    _central_like(settings)
    credential = issue_node_registration("Node A")
    envelope = _make_envelope(source_node_id=credential.node_id)
    _node_like(settings)

    response = _post_events(client, credential.node_id, credential.secret, [envelope])

    assert response.status_code == 403
    assert InboxMessage.objects.filter(event_id=envelope.event_id).count() == 0


def test_node_profile_rejects_manage_sync_node(settings):
    _node_like(settings)

    with pytest.raises(CommandError):
        call_command("manage_sync_node", "issue", "--name", "Lab A")


def test_node_profile_permits_sync_worker(settings):
    _node_like(settings)
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(uuid.uuid4())
    settings.LINUXLAB_SYNC_SECRET = "the-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    transport = SyncEventsCommand()._build_transport()

    assert transport is not None


# --- Central profile: ingestion/provisioning available, worker rejected ----


def test_central_profile_permits_ingestion_and_provisioning(client, settings):
    _central_like(settings)
    credential = issue_node_registration("Node A")
    envelope = _make_envelope(source_node_id=credential.node_id)

    response = _post_events(client, credential.node_id, credential.secret, [envelope])

    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["results"][0]["status"] == ACCEPTED
    assert InboxMessage.objects.filter(event_id=envelope.event_id).exists()


def test_central_profile_rejects_sync_worker(settings):
    _central_like(settings)
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(uuid.uuid4())
    settings.LINUXLAB_SYNC_SECRET = "the-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    with pytest.raises(CommandError):
        SyncEventsCommand()._build_transport()

    with pytest.raises(CommandError):
        call_command("sync_events", "--once")


# --- real deployment profiles (not just simulated flags) -------------------


def test_real_central_settings_module_permits_ingestion_only():
    from linuxlab.settings import central

    assert central.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS is True
    assert central.LINUXLAB_CAN_RUN_SYNC_WORKER is False


def test_real_node_settings_module_permits_worker_only():
    from linuxlab.settings import node

    assert node.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS is False
    assert node.LINUXLAB_CAN_RUN_SYNC_WORKER is True
