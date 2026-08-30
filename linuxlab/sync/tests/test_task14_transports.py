"""Task 14 SyncTransport implementations (T14-D01, T14-D02, T14-D29,
T14-D39): HttpSyncTransport is the production transport (HTTPS-only,
certificate-verified, per-event ACK parsing); FileSyncTransport is
dev/test/reference only and can never itself prove production delivery.
"""

import json
import urllib.error
import uuid
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from linuxlab.sync.contracts import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    AuthTransportError,
    ProtocolTransportError,
    TransientTransportError,
    WireEventEnvelope,
)
from linuxlab.sync.transports import FileSyncTransport, HttpSyncTransport


def _make_envelope(**overrides):
    fields = {
        "event_id": uuid.uuid4(),
        "event_type": "activity.completed",
        "occurred_at": timezone.now(),
        "source_node_id": uuid.uuid4(),
        "aggregate_type": "attempt",
        "aggregate_public_id": uuid.uuid4(),
        "schema_version": 1,
        "payload": {"score": 1, "max_score": 1},
    }
    fields.update(overrides)
    return WireEventEnvelope(**fields)


def _fake_http_response(status, body_dict):
    response = MagicMock()
    response.status = status
    response.read.return_value = json.dumps(body_dict).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


# --- HTTPS policy -------------------------------------------------------


def test_http_transport_rejects_plain_http_by_default():
    with pytest.raises(ValueError):
        HttpSyncTransport("http://central.example.org", uuid.uuid4(), "secret")


def test_http_transport_allows_https():
    HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")


def test_http_transport_allows_http_only_with_explicit_insecure_flag():
    HttpSyncTransport(
        "http://central.example.org",
        uuid.uuid4(),
        "secret",
        allow_insecure_http=True,
    )


def test_http_transport_rejects_unsupported_scheme():
    with pytest.raises(ValueError):
        HttpSyncTransport("ftp://central.example.org", uuid.uuid4(), "secret")


def test_http_transport_repr_never_includes_secret():
    node_id = uuid.uuid4()
    transport = HttpSyncTransport(
        "https://central.example.org", node_id, "super-secret-value"
    )

    assert "super-secret-value" not in repr(transport)


def test_http_transport_sends_bearer_secret_and_node_id_as_headers():
    node_id = uuid.uuid4()
    envelope = _make_envelope()
    transport = HttpSyncTransport("https://central.example.org", node_id, "the-secret")

    captured_request = {}

    def _fake_urlopen(request, timeout=None):
        captured_request["headers"] = dict(request.header_items())
        captured_request["body"] = json.loads(request.data)
        return _fake_http_response(
            200, {"results": [{"event_id": str(envelope.event_id), "status": ACCEPTED}]}
        )

    with patch("linuxlab.sync.transports.urllib.request.urlopen", side_effect=_fake_urlopen):
        transport.send_batch((envelope,))

    headers = {k.lower(): v for k, v in captured_request["headers"].items()}
    assert headers["authorization"] == "Bearer the-secret"
    assert headers["x-linuxlab-node-id"] == str(node_id)
    assert "the-secret" not in json.dumps(captured_request["body"])


def test_http_transport_never_falls_back_from_https_to_http():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()

    calls = []

    def _fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        raise urllib.error.URLError("connection refused")

    with (
        patch("linuxlab.sync.transports.urllib.request.urlopen", side_effect=_fake_urlopen),
        pytest.raises(TransientTransportError),
    ):
        transport.send_batch((envelope,))

    assert calls == ["https://central.example.org/api/v1/sync/events/"]
    assert all(url.startswith("https://") for url in calls)


# --- ACK parsing ----------------------------------------------------------


def test_http_transport_parses_exact_per_event_results():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    e1, e2, e3 = _make_envelope(), _make_envelope(), _make_envelope()
    body = {
        "results": [
            {"event_id": str(e1.event_id), "status": ACCEPTED},
            {"event_id": str(e2.event_id), "status": DUPLICATE},
            {
                "event_id": str(e3.event_id),
                "status": REJECTED,
                "code": "IDEMPOTENCY_CONFLICT",
                "reason": "content mismatch",
            },
        ]
    }

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen",
        return_value=_fake_http_response(200, body),
    ):
        ack = transport.send_batch((e1, e2, e3))

    assert ack.status_for(e1.event_id) == ACCEPTED
    assert ack.status_for(e2.event_id) == DUPLICATE
    assert ack.status_for(e3.event_id) == REJECTED


def test_http_transport_treats_unknown_status_as_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = {"results": [{"event_id": str(envelope.event_id), "status": "bogus-status"}]}

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen",
        return_value=_fake_http_response(200, body),
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_http_transport_missing_event_result_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    present, missing = _make_envelope(), _make_envelope()
    body = {"results": [{"event_id": str(present.event_id), "status": ACCEPTED}]}

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen",
        return_value=_fake_http_response(200, body),
    ):
        ack = transport.send_batch((present, missing))

    assert ack.status_for(present.event_id) == ACCEPTED
    assert ack.status_for(missing.event_id) is None


def test_http_transport_malformed_json_response_is_transient():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    response = MagicMock()
    response.status = 200
    response.read.return_value = b"{not valid json"
    response.__enter__.return_value = response
    response.__exit__.return_value = False

    with (
        patch("linuxlab.sync.transports.urllib.request.urlopen", return_value=response),
        pytest.raises(TransientTransportError),
    ):
        transport.send_batch((envelope,))


def test_http_transport_generic_2xx_without_results_never_marks_all_delivered():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()

    with (
        patch(
            "linuxlab.sync.transports.urllib.request.urlopen",
            return_value=_fake_http_response(200, {"ok": True}),
        ),
        pytest.raises(TransientTransportError),
    ):
        transport.send_batch((envelope,))


# --- HTTP failure classification -------------------------------------------


def _http_error(status, body=b"{}"):
    return urllib.error.HTTPError(
        url="https://central.example.org/api/v1/sync/events/",
        code=status,
        msg="error",
        hdrs=None,
        fp=None,
    )


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_is_transient(status):
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    error = _http_error(status)
    error.read = lambda: b"{}"

    with (
        patch("linuxlab.sync.transports.urllib.request.urlopen", side_effect=error),
        pytest.raises(TransientTransportError),
    ):
        transport.send_batch((envelope,))


@pytest.mark.parametrize("status", [401, 403])
def test_401_403_is_auth_fault(status):
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    error = _http_error(status)
    error.read = lambda: b"{}"

    with (
        patch("linuxlab.sync.transports.urllib.request.urlopen", side_effect=error),
        pytest.raises(AuthTransportError),
    ):
        transport.send_batch((envelope,))


@pytest.mark.parametrize("status", [400, 413])
def test_400_413_is_protocol_failure(status):
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    error = _http_error(status)
    error.read = lambda: b"{}"

    with (
        patch("linuxlab.sync.transports.urllib.request.urlopen", side_effect=error),
        pytest.raises(ProtocolTransportError),
    ):
        transport.send_batch((envelope,))


def test_timeout_is_transient():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()

    with (
        patch(
            "linuxlab.sync.transports.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ),
        pytest.raises(TransientTransportError),
    ):
        transport.send_batch((envelope,))


def test_dns_connect_failure_is_transient():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()

    with (
        patch(
            "linuxlab.sync.transports.urllib.request.urlopen",
            side_effect=urllib.error.URLError("name resolution failed"),
        ),
        pytest.raises(TransientTransportError),
    ):
        transport.send_batch((envelope,))


# --- FileSyncTransport (dev/test/reference only) ---------------------------


def test_file_transport_writes_deterministic_canonical_batch_file(tmp_path):
    from linuxlab.sync.contracts import canonical_event_dict

    envelope = _make_envelope()
    transport = FileSyncTransport(tmp_path)

    transport.send_batch((envelope,))

    written_files = list(tmp_path.glob("*.json"))
    assert len(written_files) == 1
    content = json.loads(written_files[0].read_text())
    assert content["events"] == [canonical_event_dict(envelope)]


def test_file_transport_never_writes_a_bearer_secret(tmp_path):
    envelope = _make_envelope()
    transport = FileSyncTransport(tmp_path)

    transport.send_batch((envelope,))

    for path in tmp_path.glob("*.json"):
        assert "secret" not in path.read_text().lower()


def test_file_transport_always_returns_unconfirmed_results(tmp_path):
    envelope = _make_envelope()
    transport = FileSyncTransport(tmp_path)

    ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None
    assert ack.results == ()


def test_file_transport_empty_batch_writes_nothing(tmp_path):
    transport = FileSyncTransport(tmp_path)

    ack = transport.send_batch(())

    assert ack.results == ()
    assert list(tmp_path.glob("*.json")) == []
