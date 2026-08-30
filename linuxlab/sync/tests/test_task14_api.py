"""Task 14 authenticated sync batch API (T14-D07, T14-D08, T14-D09,
T14-D30): POST /api/v1/sync/events/.
"""

import json
import uuid

import pytest
from django.utils import timezone

from linuxlab.sync.authentication import issue_node_registration, revoke_node_registration
from linuxlab.sync.contracts import ACCEPTED, DUPLICATE, REJECTED, canonical_event_dict
from linuxlab.sync.contracts import WireEventEnvelope as _Envelope
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


def _headers(node_id, secret):
    return {
        "HTTP_AUTHORIZATION": f"Bearer {secret}",
        "HTTP_X_LINUXLAB_NODE_ID": str(node_id),
    }


def _post_events(client, node_id, secret, envelopes, **extra):
    body = json.dumps({"events": [canonical_event_dict(e) for e in envelopes]})
    headers = _headers(node_id, secret)
    headers.update(extra)
    return client.post(_URL, data=body, content_type="application/json", **headers)


# --- route / auth ------------------------------------------------------


def test_missing_bearer_header_is_401(client):
    response = client.post(_URL, data="{}", content_type="application/json")
    assert response.status_code == 401


def test_malformed_bearer_header_is_401(client):
    response = client.post(
        _URL,
        data="{}",
        content_type="application/json",
        HTTP_AUTHORIZATION="NotBearer xyz",
        HTTP_X_LINUXLAB_NODE_ID=str(uuid.uuid4()),
    )
    assert response.status_code == 401


def test_unknown_node_id_is_401(client):
    envelope = _make_envelope()
    response = _post_events(client, uuid.uuid4(), "whatever-secret", [envelope])
    assert response.status_code == 401


def test_wrong_secret_is_401(client):
    credential = issue_node_registration("Node A")
    envelope = _make_envelope(source_node_id=credential.node_id)
    response = _post_events(client, credential.node_id, "wrong-secret", [envelope])
    assert response.status_code == 401


def test_disabled_registration_is_403(client):
    credential = issue_node_registration("Node A")
    revoke_node_registration(credential.node_id)
    envelope = _make_envelope(source_node_id=credential.node_id)
    response = _post_events(client, credential.node_id, credential.secret, [envelope])
    assert response.status_code == 403


def test_authenticated_node_continues_to_ingestion(client):
    credential = issue_node_registration("Node A")
    envelope = _make_envelope(source_node_id=credential.node_id)
    response = _post_events(client, credential.node_id, credential.secret, [envelope])
    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["results"][0]["status"] == ACCEPTED


# --- request-protocol -------------------------------------------------------


def test_malformed_json_body_is_400(client):
    credential = issue_node_registration("Node A")
    response = client.post(
        _URL, data="not json", content_type="application/json", **_headers(
            credential.node_id, credential.secret
        )
    )
    assert response.status_code == 400


def test_malformed_batch_shape_is_400(client):
    credential = issue_node_registration("Node A")
    response = client.post(
        _URL,
        data=json.dumps({"not_events": []}),
        content_type="application/json",
        **_headers(credential.node_id, credential.secret),
    )
    assert response.status_code == 400


def test_malformed_individual_event_is_400_for_the_whole_batch(client):
    credential = issue_node_registration("Node A")
    good = canonical_event_dict(_make_envelope(source_node_id=credential.node_id))
    bad = {"event_id": "not-a-uuid"}
    response = client.post(
        _URL,
        data=json.dumps({"events": [good, bad]}),
        content_type="application/json",
        **_headers(credential.node_id, credential.secret),
    )
    assert response.status_code == 400
    assert InboxMessage.objects.count() == 0


def test_oversized_event_count_is_413(client, settings):
    settings.LINUXLAB_SYNC_BATCH_MAX_EVENTS = 2
    credential = issue_node_registration("Node A")
    envelopes = [_make_envelope(source_node_id=credential.node_id) for _ in range(3)]
    response = _post_events(client, credential.node_id, credential.secret, envelopes)
    assert response.status_code == 413


def test_oversized_bytes_is_413(client, settings):
    settings.LINUXLAB_SYNC_BATCH_MAX_BYTES = 10
    credential = issue_node_registration("Node A")
    envelope = _make_envelope(source_node_id=credential.node_id)
    response = _post_events(client, credential.node_id, credential.secret, [envelope])
    assert response.status_code == 413


def test_plaintext_credential_in_body_is_never_used_for_auth(client):
    credential = issue_node_registration("Node A")
    envelope = _make_envelope(source_node_id=credential.node_id)
    body = json.dumps(
        {"secret": credential.secret, "events": [canonical_event_dict(envelope)]}
    )
    response = client.post(
        _URL,
        data=body,
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer wrong-secret",
        HTTP_X_LINUXLAB_NODE_ID=str(credential.node_id),
    )
    assert response.status_code == 401


# --- mixed batch -------------------------------------------------------------


def test_mixed_batch_returns_independent_per_event_results(client):
    credential = issue_node_registration("Node A")
    good_new = _make_envelope(source_node_id=credential.node_id)
    good_duplicate = _make_envelope(source_node_id=credential.node_id)
    invalid = _make_envelope(source_node_id=credential.node_id, payload={"bogus": True})

    # Pre-seed the duplicate so this batch's copy is an exact repeat.
    _post_events(client, credential.node_id, credential.secret, [good_duplicate])

    response = _post_events(
        client, credential.node_id, credential.secret, [good_new, good_duplicate, invalid]
    )

    assert response.status_code == 200
    results = {r["event_id"]: r["status"] for r in json.loads(response.content)["results"]}
    assert results[str(good_new.event_id)] == ACCEPTED
    assert results[str(good_duplicate.event_id)] == DUPLICATE
    assert results[str(invalid.event_id)] == REJECTED
    assert InboxMessage.objects.filter(event_id=good_new.event_id).exists()
    assert InboxMessage.objects.filter(event_id=good_duplicate.event_id).count() == 1
    assert not InboxMessage.objects.filter(event_id=invalid.event_id).exists()


# --- source spoofing -----------------------------------------------------


def test_source_spoof_is_permanently_rejected_with_no_inbox_row(client):
    credential_a = issue_node_registration("Node A")
    credential_b = issue_node_registration("Node B")
    spoofed = _make_envelope(source_node_id=credential_b.node_id)

    response = _post_events(client, credential_a.node_id, credential_a.secret, [spoofed])

    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["results"][0]["status"] == REJECTED
    assert body["results"][0]["code"] == "SOURCE_MISMATCH"
    assert not InboxMessage.objects.filter(event_id=spoofed.event_id).exists()
