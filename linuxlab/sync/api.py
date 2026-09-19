"""Task 14 authenticated sync ingestion API (T14-D07, T14-D08, T14-D09,
T14-D30), plus Corrective B Task 11's dedicated read-only Central probe
(`GET /api/v1/sync/probe/`, approved spec §7).

`POST /api/v1/sync/events/` is a thin adapter: it authenticates once,
validates the batch's structural shape, then delegates every event
independently to `linuxlab.sync.ingestion.ingest_event()`. No Central
learning-domain mutation belongs here.

Request-level vs event-level semantics (T14-D30): a malformed/oversized
request never begins event-level ingest at all (400/413); once the
batch is structurally valid, the response is always 2xx and every
event gets its own independent accepted/duplicate/rejected result --
one rejected event never affects any other event in the same batch.
"""

import json
import uuid
from datetime import datetime
from typing import ClassVar

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .authentication import NodeAuthenticationError, authenticate_node
from .capabilities import can_receive_sync_events
from .configuration import SyncConfigurationError, load_inbound_limits
from .contracts import WireEventEnvelope
from .ingestion import ingest_event

_BEARER_PREFIX = "Bearer "
_AUTH_FAILED_BODY = {"detail": "authentication failed"}
_PROFILE_DISABLED_BODY = {"detail": "sync ingestion is not enabled on this deployment profile"}
_PROBE_OK_BODY = {"status": "ok"}


def _parse_bearer_credential(request):
    """Returns (node_id, secret), or (None, None) if the Authorization/
    Node-ID headers are missing or malformed -- never reads a credential
    from the JSON request body (T14-D09)."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith(_BEARER_PREFIX):
        return None, None
    secret = auth_header[len(_BEARER_PREFIX) :]
    if not secret:
        return None, None

    node_id_header = request.headers.get("X-LinuxLab-Node-Id", "")
    try:
        node_id = uuid.UUID(node_id_header)
    except (ValueError, AttributeError, TypeError):
        return None, None

    return node_id, secret


def _authenticate_request(node_id, secret):
    """Returns (NodeRegistration, None) on success, or (None, 401) on
    every credential failure (T14-D09; Corrective B Task 5): unknown
    node_id, zero/multiple ACTIVE, REVOKED/SUPERSEDED history, or a
    wrong secret are all indistinguishable to the caller -- there is no
    separate history-based status lookup here. The unrelated
    receive-capability/profile-disabled 403 stays in SyncEventsView.post."""
    try:
        registration = authenticate_node(node_id, secret)
    except NodeAuthenticationError:
        return None, 401
    return registration, None


def _parse_wire_envelope(item):
    if not isinstance(item, dict):
        raise ValueError("event must be an object")  # noqa: TRY004

    try:
        event_id = uuid.UUID(str(item["event_id"]))
        event_type = item["event_type"]
        occurred_at = datetime.fromisoformat(item["occurred_at"])
        source_node_id = uuid.UUID(str(item["source_node_id"]))
        aggregate_type = item["aggregate_type"]
        aggregate_public_id = uuid.UUID(str(item["aggregate_public_id"]))
        schema_version = item["schema_version"]
        payload = item["payload"]
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed event envelope: {exc}") from exc

    if not isinstance(event_type, str) or not event_type:
        raise ValueError("event_type must be a non-empty string")
    if not isinstance(aggregate_type, str) or not aggregate_type:
        raise ValueError("aggregate_type must be a non-empty string")
    if type(schema_version) is not int or schema_version < 1:
        raise ValueError("schema_version must be an integer >= 1")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")  # noqa: TRY004

    return WireEventEnvelope(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        source_node_id=source_node_id,
        aggregate_type=aggregate_type,
        aggregate_public_id=aggregate_public_id,
        schema_version=schema_version,
        payload=payload,
    )


def _ack_to_dict(ack):
    return {
        "event_id": str(ack.event_id),
        "status": ack.status,
        "code": ack.code,
        "reason": ack.reason,
    }


@method_decorator(csrf_exempt, name="dispatch")
class SyncEventsView(View):
    """POST /api/v1/sync/events/ -- authenticated Node-to-Central event
    batch ingestion. Bearer-token authenticated (no session/cookie
    ambient authority), so CSRF protection does not apply here."""

    def post(self, request):
        if not can_receive_sync_events():
            return JsonResponse(_PROFILE_DISABLED_BODY, status=403)

        node_id, secret = _parse_bearer_credential(request)
        if node_id is None:
            return JsonResponse(_AUTH_FAILED_BODY, status=401)

        node, error_status = _authenticate_request(node_id, secret)
        if node is None:
            return JsonResponse(_AUTH_FAILED_BODY, status=error_status)

        try:
            max_events, max_bytes = load_inbound_limits()
        except SyncConfigurationError:
            return JsonResponse({"detail": "sync configuration unavailable"}, status=503)

        if len(request.body) > max_bytes:
            return JsonResponse({"detail": "request too large"}, status=413)

        try:
            parsed = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"detail": "malformed JSON"}, status=400)

        if not isinstance(parsed, dict) or not isinstance(parsed.get("events"), list):
            return JsonResponse({"detail": "malformed batch"}, status=400)

        events = parsed["events"]
        if len(events) > max_events:
            return JsonResponse({"detail": "too many events"}, status=413)

        try:
            envelopes = [_parse_wire_envelope(item) for item in events]
        except ValueError:
            return JsonResponse({"detail": "malformed event envelope"}, status=400)

        results = [_ack_to_dict(ingest_event(node, envelope)) for envelope in envelopes]

        return JsonResponse({"results": results}, status=200)


@method_decorator(csrf_exempt, name="dispatch")
class SyncProbeView(View):
    """GET /api/v1/sync/probe/ -- Corrective B Task 11 (approved spec §7):
    a dedicated, strictly read-only Central probe. Exactly the same
    receive-capability gate, header parsing and Task-5 authenticate_node
    authority as event ingestion (no second auth interpretation), then a
    fixed HTTP 200 {"status": "ok"}. It ingests nothing and writes
    nothing: no InboxMessage/OutboxMessage/DomainEvent/registration/
    session state, no password-hash upgrade, no audit record. Every
    credential failure is the same generic 401 as ingestion; the
    unrelated profile/capability-disabled 403 stays exactly as it is.
    Strictly GET only (T11-M1): HEAD and OPTIONS are NOT routed through
    get() or auto-answered -- every other method is 405 before any
    authentication work."""

    http_method_names: ClassVar[list[str]] = ["get"]

    def get(self, request):
        if not can_receive_sync_events():
            return JsonResponse(_PROFILE_DISABLED_BODY, status=403)

        node_id, secret = _parse_bearer_credential(request)
        if node_id is None:
            return JsonResponse(_AUTH_FAILED_BODY, status=401)

        node, error_status = _authenticate_request(node_id, secret)
        if node is None:
            return JsonResponse(_AUTH_FAILED_BODY, status=error_status)

        return JsonResponse(_PROBE_OK_BODY, status=200)
