"""Task 14 authenticated sync ingestion API (T14-D07, T14-D08, T14-D09,
T14-D30).

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

from django.conf import settings
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .authentication import NodeAuthenticationError, authenticate_node
from .contracts import WireEventEnvelope
from .ingestion import ingest_event
from .models import NodeRegistration

_BEARER_PREFIX = "Bearer "
_AUTH_FAILED_BODY = {"detail": "authentication failed"}


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
    """Returns (NodeRegistration, None) on success, or (None, status)
    on failure: 401 for missing/unknown Node or a wrong secret, 403 for
    a known but disabled/revoked registration. The response BODY stays
    generic either way (T14-D09) -- only the status code differs, and a
    disabled/revoked registration is checked only AFTER
    authenticate_node() has already failed, so it never changes the
    outcome of the underlying (deliberately uniform) credential check."""
    try:
        registration = authenticate_node(node_id, secret)
    except NodeAuthenticationError:
        if NodeRegistration.objects.filter(node_id=node_id, enabled=False).exists():
            return None, 403
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
        node_id, secret = _parse_bearer_credential(request)
        if node_id is None:
            return JsonResponse(_AUTH_FAILED_BODY, status=401)

        node, error_status = _authenticate_request(node_id, secret)
        if node is None:
            return JsonResponse(_AUTH_FAILED_BODY, status=error_status)

        if len(request.body) > settings.LINUXLAB_SYNC_BATCH_MAX_BYTES:
            return JsonResponse({"detail": "request too large"}, status=413)

        try:
            parsed = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"detail": "malformed JSON"}, status=400)

        if not isinstance(parsed, dict) or not isinstance(parsed.get("events"), list):
            return JsonResponse({"detail": "malformed batch"}, status=400)

        events = parsed["events"]
        if len(events) > settings.LINUXLAB_SYNC_BATCH_MAX_EVENTS:
            return JsonResponse({"detail": "too many events"}, status=413)

        try:
            envelopes = [_parse_wire_envelope(item) for item in events]
        except ValueError:
            return JsonResponse({"detail": "malformed event envelope"}, status=400)

        results = [_ack_to_dict(ingest_event(node, envelope)) for envelope in envelopes]

        return JsonResponse({"results": results}, status=200)
