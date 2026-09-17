"""Task 14 SyncTransport implementations (T14-D01, T14-D02, T14-D29,
T14-D39): HttpSyncTransport is the production transport (HTTPS-only,
certificate-verified, per-event ACK parsing); FileSyncTransport is
dev/test/reference only and can never itself prove production delivery.
"""

import http.server
import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from email.message import Message
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from linuxlab.sync.contracts import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    AuthTransportError,
    ProtocolTransportError,
    RateLimitedTransportError,
    RequestTooLargeError,
    SyncOutcome,
    TransientTransportError,
    WireEventEnvelope,
)
from linuxlab.sync.transports import (
    FileSyncTransport,
    HttpSyncTransport,
    _NoRedirectHandler,
    classify_http_failure,
    parse_retry_after,
)


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


# --- corrective (independent review finding #7): ambiguous ACK entries
# never authorize an irreversible action -- duplicated/conflicting entries
# for one event_id, or a REJECTED entry missing its code, stay unconfirmed.


def _batch_response(client_body):
    return _fake_http_response(200, {"results": client_body})


def test_duplicate_accepted_entries_for_same_id_are_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(envelope.event_id), "status": ACCEPTED},
        {"event_id": str(envelope.event_id), "status": ACCEPTED},
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_accepted_then_rejected_for_same_id_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(envelope.event_id), "status": ACCEPTED},
        {"event_id": str(envelope.event_id), "status": REJECTED, "code": "X"},
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_rejected_then_accepted_for_same_id_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(envelope.event_id), "status": REJECTED, "code": "X"},
        {"event_id": str(envelope.event_id), "status": ACCEPTED},
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_duplicate_then_accepted_for_same_id_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(envelope.event_id), "status": DUPLICATE},
        {"event_id": str(envelope.event_id), "status": ACCEPTED},
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_rejected_missing_code_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [{"event_id": str(envelope.event_id), "status": REJECTED}]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_unknown_result_id_never_affects_sent_events():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(uuid.uuid4()), "status": ACCEPTED},
        {"event_id": str(envelope.event_id), "status": ACCEPTED},
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) == ACCEPTED


def test_mixture_of_valid_and_malformed_results_only_the_malformed_stays_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    good, malformed = _make_envelope(), _make_envelope()
    body = [
        {"event_id": str(good.event_id), "status": ACCEPTED},
        {"event_id": str(malformed.event_id), "status": REJECTED},  # missing code
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((good, malformed))

    assert ack.status_for(good.event_id) == ACCEPTED
    assert ack.status_for(malformed.event_id) is None


# --- second corrective (independent re-review finding #3): a malformed
# entry for the same event_id must not be silently discarded BEFORE
# duplicate-ID detection runs -- that would let the OTHER, otherwise-valid
# entry for that same ID slip through as if it were unambiguous.


def test_accepted_then_malformed_rejection_same_id_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(envelope.event_id), "status": ACCEPTED},
        {"event_id": str(envelope.event_id), "status": REJECTED},  # missing code
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_malformed_rejection_then_accepted_same_id_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(envelope.event_id), "status": REJECTED},  # missing code
        {"event_id": str(envelope.event_id), "status": ACCEPTED},
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_accepted_then_unknown_status_same_id_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(envelope.event_id), "status": ACCEPTED},
        {"event_id": str(envelope.event_id), "status": "bogus-status"},
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_unknown_status_then_accepted_same_id_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(envelope.event_id), "status": "bogus-status"},
        {"event_id": str(envelope.event_id), "status": ACCEPTED},
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_duplicate_then_malformed_same_id_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(envelope.event_id), "status": DUPLICATE},
        {"event_id": str(envelope.event_id), "status": REJECTED},  # missing code
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_malformed_then_duplicate_same_id_is_unconfirmed():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    body = [
        {"event_id": str(envelope.event_id), "status": REJECTED},  # missing code
        {"event_id": str(envelope.event_id), "status": DUPLICATE},
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((envelope,))

    assert ack.status_for(envelope.event_id) is None


def test_unrelated_valid_entry_is_unaffected_by_a_different_ids_ambiguity():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    good, ambiguous = _make_envelope(), _make_envelope()
    body = [
        {"event_id": str(good.event_id), "status": ACCEPTED},
        {"event_id": str(ambiguous.event_id), "status": ACCEPTED},
        {"event_id": str(ambiguous.event_id), "status": REJECTED},  # missing code
    ]

    with patch(
        "linuxlab.sync.transports.urllib.request.urlopen", return_value=_batch_response(body)
    ):
        ack = transport.send_batch((good, ambiguous))

    assert ack.status_for(good.event_id) == ACCEPTED
    assert ack.status_for(ambiguous.event_id) is None


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


# --- Corrective B Task 6: semantic transport outcome taxonomy --------------


def test_429_carries_semantic_retry_after():
    from datetime import UTC, datetime

    from linuxlab.sync.contracts import RateLimitedTransportError, SyncOutcome
    from linuxlab.sync.transports import classify_http_failure

    error = classify_http_failure(429, retry_after="120", now=datetime(2026, 9, 15, tzinfo=UTC))

    assert isinstance(error, RateLimitedTransportError)
    assert error.failure.category is SyncOutcome.RATE_LIMITED
    assert error.failure.retry_after_seconds == 120


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


def test_network_failure_never_leaks_host_or_credentials_in_error_text():
    transport = HttpSyncTransport(
        "https://central.example.org", uuid.uuid4(), "TOP-SECRET-BEARER-VALUE"
    )
    envelope = _make_envelope()

    def _boom(request, timeout=None):
        del request, timeout
        raise urllib.error.URLError(
            "Connection refused: TOP-SECRET-BEARER-VALUE at central.example.org:9999"
        )

    with (
        patch("linuxlab.sync.transports.urllib.request.urlopen", side_effect=_boom),
        pytest.raises(TransientTransportError) as excinfo,
    ):
        transport.send_batch((envelope,))

    assert "TOP-SECRET-BEARER-VALUE" not in str(excinfo.value)
    assert "TOP-SECRET-BEARER-VALUE" not in repr(excinfo.value)
    assert "central.example.org:9999" not in str(excinfo.value)
    assert "central.example.org:9999" not in repr(excinfo.value)


# --- classify_http_failure() status coverage --------------------------------


def test_classify_400_is_permanent_rejection_with_http_status():
    error = classify_http_failure(400)

    assert isinstance(error, ProtocolTransportError)
    assert error.failure.category is SyncOutcome.PERMANENT_REJECTION
    assert error.failure.http_status == 400


@pytest.mark.parametrize("status", [401, 403])
def test_classify_401_403_is_auth_or_registration_failure(status):
    error = classify_http_failure(status)

    assert isinstance(error, AuthTransportError)
    assert error.failure.category is SyncOutcome.AUTH_OR_REGISTRATION_FAILURE


def test_classify_408_is_transient():
    error = classify_http_failure(408)

    assert isinstance(error, TransientTransportError)
    assert error.failure.category is SyncOutcome.TRANSIENT_FAILURE


def test_classify_413_is_request_too_large_with_transient_and_safe_code():
    error = classify_http_failure(413)

    assert isinstance(error, RequestTooLargeError)
    assert error.failure.category is SyncOutcome.TRANSIENT_FAILURE
    assert error.failure.error_code == "BATCH_TOO_LARGE"


def test_classify_429_without_retry_after_is_still_rate_limited():
    error = classify_http_failure(429)

    assert isinstance(error, RateLimitedTransportError)
    assert error.failure.category is SyncOutcome.RATE_LIMITED
    assert error.failure.retry_after_seconds is None


@pytest.mark.parametrize("status", [500, 501, 502, 503, 599])
def test_classify_5xx_is_transient(status):
    error = classify_http_failure(status)

    assert isinstance(error, TransientTransportError)
    assert error.failure.category is SyncOutcome.TRANSIENT_FAILURE


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308])
def test_classify_3xx_is_conservatively_transient(status):
    error = classify_http_failure(status)

    assert isinstance(error, TransientTransportError)
    assert error.failure.category is SyncOutcome.TRANSIENT_FAILURE
    assert not isinstance(error, AuthTransportError)
    assert not isinstance(error, ProtocolTransportError)


@pytest.mark.parametrize("status", [402, 404, 409, 422, 451])
def test_classify_unknown_non_2xx_is_conservatively_transient_never_all_4xx_permanent(status):
    error = classify_http_failure(status)

    assert isinstance(error, TransientTransportError)
    assert error.failure.category is SyncOutcome.TRANSIENT_FAILURE
    assert not isinstance(error, ProtocolTransportError)


def test_http_transport_429_extracts_retry_after_header_from_the_real_response():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    headers = Message()
    headers["Retry-After"] = "30"
    error = urllib.error.HTTPError(
        url="https://central.example.org/api/v1/sync/events/",
        code=429,
        msg="Too Many Requests",
        hdrs=headers,
        fp=None,
    )
    error.read = lambda: b"{}"

    with (
        patch("linuxlab.sync.transports.urllib.request.urlopen", side_effect=error),
        pytest.raises(RateLimitedTransportError) as excinfo,
    ):
        transport.send_batch((envelope,))

    assert excinfo.value.failure.category is SyncOutcome.RATE_LIMITED
    assert excinfo.value.failure.retry_after_seconds == 30


def test_http_error_classification_never_leaks_raw_response_body():
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    error = _http_error(400)
    error.read = lambda: b'{"detail": "TOP-SECRET-SERVER-BODY", "authorization": "Bearer leaked"}'

    with (
        patch("linuxlab.sync.transports.urllib.request.urlopen", side_effect=error),
        pytest.raises(ProtocolTransportError) as excinfo,
    ):
        transport.send_batch((envelope,))

    assert "TOP-SECRET-SERVER-BODY" not in str(excinfo.value)
    assert "TOP-SECRET-SERVER-BODY" not in repr(excinfo.value)
    assert "TOP-SECRET-SERVER-BODY" not in repr(excinfo.value.failure)


def test_http_error_with_no_headers_object_still_classifies_safely():
    """`hdrs=None` (no headers at all on the HTTPError) must never raise
    an AttributeError while extracting Retry-After -- exercised
    incidentally by every other _http_error()-based test here, asserted
    explicitly for the 429 case where Retry-After extraction is load
    bearing."""
    transport = HttpSyncTransport("https://central.example.org", uuid.uuid4(), "secret")
    envelope = _make_envelope()
    error = _http_error(429)
    error.read = lambda: b"{}"

    with (
        patch("linuxlab.sync.transports.urllib.request.urlopen", side_effect=error),
        pytest.raises(RateLimitedTransportError) as excinfo,
    ):
        transport.send_batch((envelope,))

    assert excinfo.value.failure.retry_after_seconds is None


# --- parse_retry_after() -----------------------------------------------


def test_parse_retry_after_none_value_returns_none():
    assert parse_retry_after(None, now=datetime(2026, 9, 15, tzinfo=UTC)) is None


def test_parse_retry_after_integer_delta_seconds():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    assert parse_retry_after("120", now=now) == 120


def test_parse_retry_after_http_date_computes_delta_from_now():
    now = datetime(2026, 9, 15, 0, 0, 0, tzinfo=UTC)
    future_date = "Tue, 15 Sep 2026 00:02:00 GMT"
    result = parse_retry_after(future_date, now=now)
    assert result == pytest.approx(120, abs=1)


def test_parse_retry_after_expired_http_date_clamps_to_zero():
    now = datetime(2026, 9, 15, 0, 5, 0, tzinfo=UTC)
    past_date = "Tue, 15 Sep 2026 00:00:00 GMT"
    assert parse_retry_after(past_date, now=now) == 0


def test_parse_retry_after_negative_integer_clamps_to_zero():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    assert parse_retry_after("-5", now=now) == 0


def test_parse_retry_after_malformed_value_returns_none():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    assert parse_retry_after("not-a-valid-retry-after", now=now) is None


def test_parse_retry_after_whitespace_only_returns_none():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    assert parse_retry_after("   ", now=now) is None


def test_parse_retry_after_malformed_http_date_returns_none():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    assert parse_retry_after("Not, 99 Foo 9999 99:99:99 ZZZ", now=now) is None


def test_parse_retry_after_oversized_header_is_ignored():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    assert parse_retry_after("1" * 5000, now=now) is None


def test_parse_retry_after_excessive_integer_preserved_as_python_int_never_float():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    huge = 10**30
    result = parse_retry_after(str(huge), now=now)
    assert result == huge
    assert isinstance(result, int)


def test_parse_retry_after_never_retains_the_raw_sentinel_value():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    sentinel = "SENTINEL-DO-NOT-LEAK-" + "x" * 50

    result = parse_retry_after(sentinel, now=now)

    assert result is None
    error = classify_http_failure(429, retry_after=sentinel, now=now)
    assert sentinel not in str(error)
    assert sentinel not in repr(error)
    assert sentinel not in repr(error.failure)


# --- no-redirect / Authorization safety -------------------------------------


def test_no_redirect_handler_never_follows_a_redirect():
    handler = _NoRedirectHandler()
    req = urllib.request.Request("https://central.example.org/api/v1/sync/events/")

    result = handler.redirect_request(
        req, None, 302, "Found", {}, "https://evil.example.org/steal"
    )

    assert result is None


class _SilentHTTPServer(http.server.HTTPServer):
    def handle_error(self, request, client_address):
        pass


def test_redirect_is_never_followed_and_authorization_never_reaches_second_origin():
    """Real, unmocked end-to-end proof (not just a unit test of the
    handler): an origin server that answers every POST with a 302 to a
    second local server, which never receives any request at all -- the
    strongest possible proof that Authorization is never forwarded to a
    second origin, since the second origin is never contacted."""
    target_requests = []

    class _TargetHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            target_requests.append(dict(self.headers.items()))
            body = b'{"results": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_, *args):
            del format_, args

    target_server = _SilentHTTPServer(("127.0.0.1", 0), _TargetHandler)
    target_thread = threading.Thread(target=target_server.serve_forever, daemon=True)
    target_thread.start()
    target_port = target_server.server_address[1]

    class _OriginHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target_port}/api/v1/sync/events/"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format_, *args):
            del format_, args

    origin_server = _SilentHTTPServer(("127.0.0.1", 0), _OriginHandler)
    origin_thread = threading.Thread(target=origin_server.serve_forever, daemon=True)
    origin_thread.start()
    origin_port = origin_server.server_address[1]

    try:
        transport = HttpSyncTransport(
            f"http://127.0.0.1:{origin_port}",
            uuid.uuid4(),
            "TOP-SECRET-BEARER-VALUE",
            allow_insecure_http=True,
            timeout=5,
        )
        envelope = _make_envelope()

        with pytest.raises(TransientTransportError):
            transport.send_batch((envelope,))

        assert target_requests == []
    finally:
        origin_server.shutdown()
        target_server.shutdown()
        origin_thread.join(timeout=5)
        target_thread.join(timeout=5)


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
