"""Corrective B Task 11: the dedicated read-only Central probe
(`GET /api/v1/sync/probe/`, SyncProbeView) and the Node-side probe client
(probe_central / CentralProbeResult), approved spec §7. The endpoint
reuses ingestion's header parsing and Task-5 authenticate_node authority,
performs zero persistent mutation, and answers exactly {"status": "ok"}.
"""

import http.client
import http.server
import json
import socket
import ssl
import threading
import urllib.error
import urllib.request
import uuid
from contextlib import ExitStack
from email.message import Message
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import pytest
from django.contrib.sessions.models import Session

from linuxlab.core.diagnostics import DiagnosticContext, diagnose_deployment
from linuxlab.core.models import DomainEvent
from linuxlab.sync.authentication import (
    NodeRegistrationInvariantError,
    issue_node_registration,
    revoke_node_registration,
    rotate_node_credential,
)
from linuxlab.sync.configuration import (
    NodeCredentials,
    SyncConfigurationError,
    load_node_credentials,
)
from linuxlab.sync.diagnostics import SyncDiagnosticProvider
from linuxlab.sync.models import (
    REGISTRATION_SUPERSEDED,
    InboxMessage,
    NodeRegistration,
    OutboxMessage,
    _insert_immutable_row,
)
from linuxlab.sync.probe import CentralProbeResult, probe_central
from linuxlab.sync.transports import _TRANSPORT_OPENER

pytestmark = pytest.mark.django_db

PROBE_URL = "/api/v1/sync/probe/"
_SNAPSHOT_MODELS = (NodeRegistration, InboxMessage, OutboxMessage, DomainEvent, Session)


def _snapshot():
    return {
        model.__name__: list(model.objects.order_by("pk").values()) for model in _SNAPSHOT_MODELS
    }


def _probe_headers(node_id, secret):
    return {
        "HTTP_X_LINUXLAB_NODE_ID": str(node_id),
        "HTTP_AUTHORIZATION": f"Bearer {secret}",
    }


@pytest.fixture
def central(settings):
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True


def test_probe_accepts_active_credentials_without_mutation(client, central):
    credential = issue_node_registration("Probe Node")
    before = _snapshot()

    response = client.get(
        PROBE_URL,
        HTTP_X_LINUXLAB_NODE_ID=str(credential.node_id),
        HTTP_AUTHORIZATION=f"Bearer {credential.secret}",
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert _snapshot() == before


# --- server-side authority, status semantics, read-only contract -----------


AUTH_FAILED_BODY = {"detail": "authentication failed"}
BEARER_SENTINEL = "BEARER_SECRET_SENTINEL_51c0"
_MUTATION_TARGETS = (
    "linuxlab.sync.authentication.issue_node_registration",
    "linuxlab.sync.authentication.rotate_node_credential",
    "linuxlab.sync.authentication.revoke_node_registration",
    "linuxlab.sync.outbox.requeue_outbox_message",
    "linuxlab.sync.outbox.flush_outbox",
    "linuxlab.core.events.publish_event",
)


def _get_probe(client, node_id, secret):
    with ExitStack() as stack:
        spies = [
            stack.enter_context(patch(target, side_effect=AssertionError("mutation")))
            for target in _MUTATION_TARGETS
        ]
        response = client.get(PROBE_URL, **_probe_headers(node_id, secret))
    for spy in spies:
        spy.assert_not_called()
    return response


def _assert_generic_rejection(response, before, caplog, *sentinels):
    assert response.status_code == 401
    assert response.json() == AUTH_FAILED_BODY
    assert _snapshot() == before
    text = response.content.decode() + caplog.text
    for sentinel in sentinels:
        assert sentinel not in text
    for word in ("revoked", "superseded", "unknown", "multiple", "history", "hash"):
        assert word not in response.content.decode().lower()


def test_probe_accepted_credential_leaves_hash_unchanged_and_creates_no_session(client, central):
    credential = issue_node_registration("Probe Node")
    registration = NodeRegistration.objects.get()
    hash_before = registration.current_secret_hash
    before = _snapshot()

    response = _get_probe(client, credential.node_id, credential.secret)

    assert response.status_code == 200
    assert NodeRegistration.objects.get(pk=registration.pk).current_secret_hash == hash_before
    assert Session.objects.count() == 0
    assert "sessionid" not in response.cookies
    assert _snapshot() == before


def test_probe_missing_headers_is_generic_401(client, central, caplog):
    issue_node_registration("Probe Node")
    before = _snapshot()
    response = client.get(PROBE_URL)
    _assert_generic_rejection(response, before, caplog)


@pytest.mark.parametrize(
    "authorization,node_id",
    [
        ("Basic dXNlcjpwdw==", None),
        ("Bearer ", None),
        (f"Bearer {BEARER_SENTINEL}", "not-a-uuid"),
        (f"Bearer {BEARER_SENTINEL}", ""),
    ],
)
def test_probe_malformed_credentials_are_generic_401(client, central, caplog, authorization, node_id):
    issue_node_registration("Probe Node")
    before = _snapshot()
    headers = {"HTTP_AUTHORIZATION": authorization}
    if node_id is not None:
        headers["HTTP_X_LINUXLAB_NODE_ID"] = node_id
    response = client.get(PROBE_URL, **headers)
    _assert_generic_rejection(response, before, caplog, BEARER_SENTINEL)


def test_probe_unknown_node_is_generic_401(client, central, caplog):
    issue_node_registration("Probe Node")
    before = _snapshot()
    response = _get_probe(client, uuid.uuid4(), BEARER_SENTINEL)
    _assert_generic_rejection(response, before, caplog, BEARER_SENTINEL)


def test_probe_wrong_secret_is_generic_401(client, central, caplog):
    credential = issue_node_registration("Probe Node")
    before = _snapshot()
    response = _get_probe(client, credential.node_id, BEARER_SENTINEL)
    _assert_generic_rejection(response, before, caplog, BEARER_SENTINEL, credential.secret)


def test_probe_revoked_credential_is_generic_401(client, central, caplog):
    credential = issue_node_registration("Probe Node")
    revoke_node_registration(credential.node_id)
    before = _snapshot()
    response = _get_probe(client, credential.node_id, credential.secret)
    _assert_generic_rejection(response, before, caplog, credential.secret)


def test_probe_superseded_old_secret_is_generic_401_and_new_secret_accepted(client, central, caplog):
    old = issue_node_registration("Probe Node")
    new = rotate_node_credential(old.node_id)
    before = _snapshot()

    rejected = _get_probe(client, old.node_id, old.secret)
    _assert_generic_rejection(rejected, before, caplog, old.secret)

    accepted = _get_probe(client, new.node_id, new.secret)
    assert accepted.status_code == 200
    assert _snapshot() == before


def test_probe_history_only_node_is_generic_401(client, central, caplog):
    node_id = uuid.uuid4()
    _insert_immutable_row(
        NodeRegistration,
        node_id=node_id,
        display_name="History only",
        state=REGISTRATION_SUPERSEDED,
        current_secret_hash="!HASH_SENTINEL_history",
        previous_secret_hash="",
        previous_valid_until=None,
        enabled=True,
    )
    before = _snapshot()
    response = _get_probe(client, node_id, BEARER_SENTINEL)
    _assert_generic_rejection(response, before, caplog, BEARER_SENTINEL, "HASH_SENTINEL")


def test_probe_multiple_active_defense_path_is_generic_401(client, central, caplog):
    credential = issue_node_registration("Probe Node")
    before = _snapshot()
    with patch(
        "linuxlab.sync.authentication.get_active_node_registration",
        side_effect=NodeRegistrationInvariantError("MULTIPLE_ACTIVE_SENTINEL"),
    ):
        response = client.get(PROBE_URL, **_probe_headers(credential.node_id, credential.secret))
    _assert_generic_rejection(response, before, caplog, "MULTIPLE_ACTIVE_SENTINEL")


def test_all_rejections_are_byte_identical(client, central):
    credential = issue_node_registration("Probe Node")
    revoked = issue_node_registration("Revoked Node")
    revoke_node_registration(revoked.node_id)
    responses = [
        client.get(PROBE_URL),
        client.get(PROBE_URL, **_probe_headers(uuid.uuid4(), "x")),
        client.get(PROBE_URL, **_probe_headers(credential.node_id, "wrong")),
        client.get(PROBE_URL, **_probe_headers(revoked.node_id, revoked.secret)),
    ]
    assert {r.status_code for r in responses} == {401}
    assert len({r.content for r in responses}) == 1


# --- profile / capability / method restrictions -----------------------------


def test_probe_receive_capability_disabled_is_403_not_401(client, settings, caplog):
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("Probe Node")
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    before = _snapshot()
    with patch("linuxlab.sync.api.authenticate_node") as auth:
        response = client.get(PROBE_URL, **_probe_headers(credential.node_id, credential.secret))
    assert response.status_code == 403
    assert response.json() == {"detail": "sync ingestion is not enabled on this deployment profile"}
    auth.assert_not_called()
    assert _snapshot() == before
    assert credential.secret not in caplog.text


def test_probe_on_node_profile_is_403(client, settings):
    settings.LINUXLAB_PROFILE = "node"
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    response = client.get(PROBE_URL, **_probe_headers(uuid.uuid4(), "x"))
    assert response.status_code == 403


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_probe_non_get_methods_are_405_without_auth_side_effects(client, central, method):
    credential = issue_node_registration("Probe Node")
    before = _snapshot()
    with patch("linuxlab.sync.api.authenticate_node") as auth:
        response = getattr(client, method)(
            PROBE_URL, data="{}", content_type="application/json",
            **_probe_headers(credential.node_id, credential.secret),
        )
    assert response.status_code == 405
    auth.assert_not_called()
    assert _snapshot() == before


def test_probe_route_name_and_position(client):
    from django.urls import resolve, reverse

    from linuxlab.sync.api import SyncProbeView

    assert reverse("sync-probe") == PROBE_URL
    assert resolve(PROBE_URL).func.view_class is SyncProbeView


# --- readiness must remain local/offline while Central is failing -----------


def test_readiness_unaffected_while_central_probe_network_fails(client, settings):
    settings.LINUXLAB_PROFILE = "node"
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(uuid.uuid4())
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    with patch("urllib.request.OpenerDirector.open", side_effect=OSError("Central offline")):
        responses = [client.get(path) for path in ("/ready/", "/readyz")]

    assert [r.status_code for r in responses] == [200, 200]
    assert [r.json() for r in responses] == [{"status": "ok"}] * 2


# --- Node-side client: probe_central / CentralProbeResult -------------------


TOKEN_SENTINEL = "PROBE_TOKEN_SENTINEL_a9e4"
BODY_SENTINEL = "RESPONSE_BODY_SENTINEL_2f7b"


def _credentials(url="https://central.example.org", **overrides):
    fields = {
        "node_id": uuid.uuid4(),
        "secret": TOKEN_SENTINEL,
        "central_url": url,
        "allow_insecure_http": False,
    }
    fields.update(overrides)
    return NodeCredentials(**fields)


class _FakeResponse:
    def __init__(self, status=200, body=b'{"status": "ok"}'):
        self.status = status
        self._body = body
        self.read_requests = []

    def read(self, amount=None):
        self.read_requests.append(amount)
        return self._body if amount is None else self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, *, response=None, exception=None):
        self._response = response
        self._exception = exception
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        if self._exception is not None:
            raise self._exception
        return self._response


def _http_error(code, body=b"", headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://central.example.org/api/v1/sync/probe/", code, "status", message, None
    )


def _rendered(result, *extra):
    return " ".join([repr(result), str(result), *extra])


def test_probe_central_success_uses_exact_url_headers_method_and_timeout():
    opener = _FakeOpener(response=_FakeResponse())
    credentials = _credentials("https://central.example.org/")

    result = probe_central(credentials, opener=opener)

    assert result == CentralProbeResult(reachable=True, accepted=True, http_status=200)
    assert result.error_code == ""
    ((request, timeout),) = opener.calls
    assert timeout == 30
    assert request.full_url == "https://central.example.org/api/v1/sync/probe/"
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.get_header("Authorization") == f"Bearer {TOKEN_SENTINEL}"
    assert request.get_header("X-linuxlab-node-id") == str(credentials.node_id)
    assert TOKEN_SENTINEL not in request.full_url
    assert "?" not in request.full_url and "#" not in request.full_url and "@" not in request.full_url


def test_probe_central_reads_at_most_4097_bytes():
    response = _FakeResponse()
    probe_central(_credentials(), opener=_FakeOpener(response=response))
    assert response.read_requests == [4097]


def test_probe_central_result_never_carries_the_token():
    result = probe_central(_credentials(), opener=_FakeOpener(response=_FakeResponse()))
    assert TOKEN_SENTINEL not in _rendered(result)


@pytest.mark.parametrize("status", [401, 403])
def test_probe_central_auth_rejection_is_reachable_not_accepted(status):
    opener = _FakeOpener(exception=_http_error(status, body=BODY_SENTINEL.encode()))

    result = probe_central(_credentials(), opener=opener)

    assert result == CentralProbeResult(
        reachable=True, accepted=False, http_status=status, error_code="AUTH_REJECTED"
    )
    assert len(opener.calls) == 1
    assert BODY_SENTINEL not in _rendered(result)


@pytest.mark.parametrize("status", [301, 302, 307, 408, 429, 500, 503])
def test_probe_central_other_http_statuses_are_reachable_not_accepted(status):
    opener = _FakeOpener(
        exception=_http_error(status, headers={"Location": "https://evil.example.org/"})
    )

    result = probe_central(_credentials(), opener=opener)

    assert result.reachable is True
    assert result.accepted is False
    assert result.http_status == status
    assert result.error_code == "PROBE_RESPONSE_INVALID"
    assert len(opener.calls) == 1  # no automatic retry


def test_probe_central_non_200_success_status_is_not_accepted():
    result = probe_central(_credentials(), opener=_FakeOpener(response=_FakeResponse(status=204, body=b"")))
    assert result == CentralProbeResult(
        reachable=True, accepted=False, http_status=204, error_code="PROBE_RESPONSE_INVALID"
    )


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        BODY_SENTINEL.encode(),
        b'{"status": "ok", "extra": 1}',
        b'{"status": "OK"}',
        b'{"status": "ok "}',
        b'{"Status": "ok"}',
        b'{"status": true}',
        b'["ok"]',
        b'"ok"',
        b"null",
        b'{"status": "ok", "status": "ok"}',
        b'{"status": "ok"}\x00',
        b'{"status": "ok"' + b" " * 4090 + b"}",  # > 4096 bytes
    ],
)
def test_probe_central_rejects_every_non_exact_200_body(body):
    opener = _FakeOpener(response=_FakeResponse(status=200, body=body))

    result = probe_central(_credentials(), opener=opener)

    assert result == CentralProbeResult(
        reachable=True, accepted=False, http_status=200, error_code="PROBE_RESPONSE_INVALID"
    )
    assert BODY_SENTINEL not in _rendered(result)


def test_probe_central_accepts_exact_object_padded_to_the_4096_byte_boundary():
    body = b'{"status": "ok"' + b" " * (4096 - len(b'{"status": "ok"}')) + b"}"
    assert len(body) == 4096

    result = probe_central(_credentials(), opener=_FakeOpener(response=_FakeResponse(body=body)))

    assert result.accepted is True


@pytest.mark.parametrize(
    "exception",
    [
        urllib.error.URLError(socket.gaierror(8, f"DNS failure {TOKEN_SENTINEL}")),
        urllib.error.URLError(ssl.SSLCertVerificationError(f"TLS failure {TOKEN_SENTINEL}")),
        ssl.SSLError(f"TLS handshake {TOKEN_SENTINEL}"),
        TimeoutError(f"timed out {TOKEN_SENTINEL}"),
        ConnectionRefusedError(f"refused {TOKEN_SENTINEL}"),
        OSError(f"network down Authorization: Bearer {TOKEN_SENTINEL}"),
    ],
)
def test_probe_central_network_failures_are_unreachable(exception, caplog):
    opener = _FakeOpener(exception=exception)

    result = probe_central(_credentials(), opener=opener)

    assert result == CentralProbeResult(
        reachable=False, accepted=False, http_status=None, error_code="PROBE_UNREACHABLE"
    )
    assert len(opener.calls) == 1
    assert TOKEN_SENTINEL not in _rendered(result, caplog.text)


def test_probe_central_insecure_http_without_flag_is_config_invalid_before_network():
    opener = _FakeOpener(response=_FakeResponse())

    result = probe_central(_credentials("http://central.example.org"), opener=opener)

    assert result == CentralProbeResult(reachable=False, accepted=False, error_code="CONFIG_INVALID")
    assert opener.calls == []


def test_probe_central_insecure_http_with_explicit_dev_flag_is_allowed():
    opener = _FakeOpener(response=_FakeResponse())
    result = probe_central(
        _credentials("http://127.0.0.1:1", allow_insecure_http=True), opener=opener
    )
    assert result.accepted is True
    assert opener.calls[0][0].full_url == "http://127.0.0.1:1/api/v1/sync/probe/"


@pytest.mark.parametrize("bad", [None, "https://central.example.org", object()])
def test_probe_central_rejects_non_credentials_without_network(bad):
    opener = _FakeOpener(response=_FakeResponse())
    result = probe_central(bad, opener=opener)
    assert result == CentralProbeResult(reachable=False, accepted=False, error_code="CONFIG_INVALID")
    assert opener.calls == []


def test_probe_central_default_opener_is_the_transport_no_redirect_opener():
    from linuxlab.sync import transports

    with patch.object(transports._TRANSPORT_OPENER, "open", side_effect=OSError("offline")) as opened:
        result = probe_central(_credentials())

    assert result.error_code == "PROBE_UNREACHABLE"
    assert opened.call_args.kwargs == {"timeout": 30}
    assert "open" not in vars(transports._TRANSPORT_OPENER)


def test_probe_central_never_follows_a_redirect_or_forwards_authorization():
    """Real end-to-end proof over loopback (the same Task-6 origin/target
    server pair used by the transport redirect tests): the origin answers
    the probe GET with a 302 to a second server, which must receive ZERO
    requests; the origin sees exactly one GET carrying the Authorization
    header; the probe result is reachable but not accepted."""
    from linuxlab.sync.tests.test_task14_transports import _start_target_and_origin_servers

    target_requests, origin_requests, _target_port, origin_port, cleanup = (
        _start_target_and_origin_servers()
    )
    try:
        credentials = _credentials(f"http://127.0.0.1:{origin_port}", allow_insecure_http=True)

        result = probe_central(credentials)

        assert origin_requests == ["GET"]
        assert target_requests == []
        assert result.reachable is True
        assert result.accepted is False
        assert result.http_status == 302
        assert TOKEN_SENTINEL not in _rendered(result)
    finally:
        cleanup()


def test_central_probe_result_is_frozen_and_safe():
    result = CentralProbeResult(reachable=True, accepted=True, http_status=200)
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is the dataclass contract
        result.accepted = False
    assert json.loads(json.dumps(result.__dict__)) == {
        "reachable": True,
        "accepted": True,
        "http_status": 200,
        "error_code": "",
    }


# --- Task-11 corrective patch: T11-C1 / T11-I1 / T11-M1 ----------------------


_OK_BODY = b'{"status": "ok"}'


class _QuietServer(http.server.HTTPServer):
    def handle_error(self, request, client_address):
        pass


def _serve(handler_class):
    """Start a loopback HTTP server; returns (port, requests, cleanup)."""
    requests = []
    handler_class.requests = requests
    server = _QuietServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def cleanup():
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return server.server_address[1], requests, cleanup


class _BaseHandler(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list] = []

    def log_message(self, format_, *args):
        del format_, args

    def _record(self):
        self.requests.append((self.command, self.path, dict(self.headers.items())))


# T11-C1 -----------------------------------------------------------------------


class _HealthLeakHandler(_BaseHandler):
    """Answers the unrelated /health/ path (any query) with the probe's
    success object WITHOUT authentication, and everything else with the
    generic 401 -- so a misbuilt probe URL that lands on /health/ would
    falsely "prove" acceptance."""

    def do_GET(self):
        self._record()
        if self.path.split("?", 1)[0] == "/health/":
            body = _OK_BODY
            self.send_response(200)
        else:
            body = b'{"detail": "authentication failed"}'
            self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_query_bearing_base_url_can_never_satisfy_auth_via_an_unrelated_path():
    port, requests, cleanup = _serve(_HealthLeakHandler)
    try:
        credentials = _credentials(
            f"http://127.0.0.1:{port}/health/?deployment=lab", allow_insecure_http=True
        )

        result = probe_central(credentials)

        assert result.accepted is False
        for _method, path, _headers in requests:
            assert "?" not in path
            assert path.endswith("/api/v1/sync/probe/")
            assert path.split("?", 1)[0] != "/health/"
    finally:
        cleanup()


def test_probe_url_is_built_as_a_path_component_never_after_a_query():
    opener = _FakeOpener(response=_FakeResponse(status=401, body=b""))
    credentials = _credentials("https://central.example.org/base/?x=1", allow_insecure_http=False)

    result = probe_central(credentials, opener=opener)

    assert result.accepted is False
    for request, _timeout in opener.calls:
        assert request.full_url == "https://central.example.org/base/api/v1/sync/probe/"


@pytest.mark.parametrize(
    "url",
    [
        "https://central.example.org/?deployment=lab",
        "https://central.example.org/health/?deployment=lab",
        "https://central.example.org?x=1",
    ],
)
def test_configuration_authority_rejects_query_bearing_central_url(url):
    source = SimpleNamespace(
        LINUXLAB_NODE_ID=str(uuid.uuid4()),
        LINUXLAB_SYNC_SECRET="secret",
        LINUXLAB_CENTRAL_URL=url,
        LINUXLAB_SYNC_ALLOW_INSECURE_HTTP=False,
    )
    with pytest.raises(SyncConfigurationError) as caught:
        load_node_credentials(settings_obj=source)
    assert caught.value.setting_name == "LINUXLAB_CENTRAL_URL"
    assert "deployment" not in str(caught.value)


# T11-I1 -----------------------------------------------------------------------


class _StalledBodyHandler(_BaseHandler):
    """Valid HTTP 200 headers, then the body never arrives."""

    release = threading.Event()

    def do_GET(self):
        self._record()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "16")
        self.end_headers()
        self.wfile.flush()
        self.release.wait(timeout=10)


class _ShortSocketTimeoutOpener:
    """Delegates to the production no-redirect opener but with a short
    socket timeout so a stalled body surfaces in test time; records the
    timeout probe_central actually requested (must be exactly 30)."""

    def __init__(self):
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append(timeout)
        return _TRANSPORT_OPENER.open(request, timeout=0.5)


def test_http_200_headers_then_stalled_body_is_reachable_not_accepted():
    port, requests, cleanup = _serve(_StalledBodyHandler)
    try:
        opener = _ShortSocketTimeoutOpener()
        credentials = _credentials(f"http://127.0.0.1:{port}", allow_insecure_http=True)

        result = probe_central(credentials, opener=opener)

        assert opener.calls == [30]
        assert len(requests) == 1  # no retry
        assert result == CentralProbeResult(
            reachable=True, accepted=False, http_status=200, error_code="PROBE_RESPONSE_INVALID"
        )
        assert TOKEN_SENTINEL not in _rendered(result)
    finally:
        _StalledBodyHandler.release.set()
        cleanup()


class _IncompleteChunkedHandler(_BaseHandler):
    """HTTP 200 with Transfer-Encoding: chunked whose chunk framing claims
    more bytes than are ever sent, then closes -- http.client raises
    IncompleteRead while reading the body."""

    def do_GET(self):
        self._record()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(b"40\r\n" + _OK_BODY)  # claims 64 bytes, sends 16, then EOF
        self.wfile.flush()
        self.close_connection = True


def test_malformed_chunked_200_is_reachable_not_accepted_without_escaping():
    port, requests, cleanup = _serve(_IncompleteChunkedHandler)
    try:
        credentials = _credentials(f"http://127.0.0.1:{port}", allow_insecure_http=True)

        result = probe_central(credentials)  # production opener, real socket

        assert len(requests) == 1
        assert result == CentralProbeResult(
            reachable=True, accepted=False, http_status=200, error_code="PROBE_RESPONSE_INVALID"
        )
    finally:
        cleanup()


@pytest.mark.parametrize(
    "read_exception",
    [
        http.client.IncompleteRead(b"partial"),
        TimeoutError("body stalled"),
        ConnectionResetError("reset mid-body"),
        ssl.SSLError("tls error mid-body"),
    ],
)
def test_body_read_failures_after_headers_keep_reachability_and_status(read_exception):
    class _FailingReadResponse(_FakeResponse):
        def read(self, amount=None):
            raise read_exception

    opener = _FakeOpener(response=_FailingReadResponse(status=200))

    result = probe_central(_credentials(), opener=opener)

    assert result == CentralProbeResult(
        reachable=True, accepted=False, http_status=200, error_code="PROBE_RESPONSE_INVALID"
    )
    assert len(opener.calls) == 1


def test_malformed_chunked_200_does_not_collapse_local_checks_to_provider_failed(
    settings, monkeypatch, tmp_path
):
    port, _requests, cleanup = _serve(_IncompleteChunkedHandler)
    try:
        settings.LINUXLAB_PROFILE = "node"
        settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
        settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
        settings.LINUXLAB_SYNC_ENABLED = True
        settings.LINUXLAB_NODE_ID = str(uuid.uuid4())
        settings.LINUXLAB_SYNC_SECRET = TOKEN_SENTINEL
        settings.LINUXLAB_SYNC_ALLOW_INSECURE_HTTP = True
        settings.LINUXLAB_CENTRAL_URL = f"http://127.0.0.1:{port}"
        settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = False
        context = DiagnosticContext(
            "node", "remote", frozenset({"LINUXLAB_CAN_RUN_SYNC_WORKER"})
        )

        report = diagnose_deployment(context, providers=[SyncDiagnosticProvider()])

        checks = {c.code: c for c in report.checks}
        assert checks["NODE_REGISTRATION"].status == "WARN"
        assert checks["SYNC_RETRY_POLICY"].status == "PASS"
        assert checks["CENTRAL_CONNECTIVITY"].status == "PASS"
        assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "FAIL"
        assert checks["CENTRAL_AUTH_ACCEPTANCE"].details["error_code"] == "PROBE_RESPONSE_INVALID"
        assert all(c.details.get("error_code") != "PROVIDER_FAILED" for c in report.checks)
        assert TOKEN_SENTINEL not in repr(report)
    finally:
        cleanup()


# T11-M1 -----------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "head", "options", "post", "put", "patch", "delete"])
def test_probe_endpoint_is_strictly_get_only(client, central, method):
    credential = issue_node_registration("Probe Node")
    before = _snapshot()
    with patch("linuxlab.sync.api.authenticate_node", wraps=None) as auth:
        auth.side_effect = AssertionError("must not authenticate")
        if method == "get":
            auth.side_effect = None
            from linuxlab.sync.authentication import authenticate_node as real_auth

            auth.side_effect = real_auth
        response = getattr(client, method)(
            PROBE_URL, **_probe_headers(credential.node_id, credential.secret)
        )
    if method == "get":
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    else:
        assert response.status_code == 405
        auth.assert_not_called()
    assert _snapshot() == before
