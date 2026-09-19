"""Node-side read-only Central probe client (Corrective B Task 11,
approved spec §7): `probe_central()` issues exactly one GET to the
configured Central's dedicated `/api/v1/sync/probe/` endpoint and maps the
outcome to concrete diagnostic evidence, `CentralProbeResult`.

This is NOT the Task-6 retry taxonomy and never triggers a retry: one
request, one result. Credentials travel only as request headers (bearer
Authorization + X-LinuxLab-Node-Id) -- never in the URL, query, fragment,
userinfo or body. Transport safety is Task 6's: the transport-owned
no-redirect opener (`_TRANSPORT_OPENER`), urllib's unconditional HTTPS
certificate verification, plain HTTP only under the existing explicit
dev/test flag, a fixed 30-second timeout. A 3xx is a failed probe, never
followed. The response body is read to at most 4097 bytes, rejected when
it exceeds 4096, and must be exactly the documented success object
{"status": "ok"} before acceptance is claimed. No raw body, header or
exception text is ever retained in the result.

Concrete evidence mapping:
    200 + exact body            -> reachable=True,  accepted=True
    401/403                     -> reachable=True,  accepted=False, AUTH_REJECTED
    any other HTTP response     -> reachable=True,  accepted=False, PROBE_RESPONSE_INVALID
    malformed/oversized 200     -> reachable=True,  accepted=False, PROBE_RESPONSE_INVALID
    DNS/TLS/timeout/network     -> reachable=False, accepted=False, PROBE_UNREACHABLE
    unusable credentials/URL    -> reachable=False, accepted=False, CONFIG_INVALID (no request)
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from .configuration import NodeCredentials
from .transports import _TRANSPORT_OPENER

_PROBE_PATH = "/api/v1/sync/probe/"
_PROBE_TIMEOUT_SECONDS = 30
_MAX_PROBE_BODY_BYTES = 4096
_SUCCESS_BODY = {"status": "ok"}

PROBE_UNREACHABLE = "PROBE_UNREACHABLE"
AUTH_REJECTED = "AUTH_REJECTED"
PROBE_RESPONSE_INVALID = "PROBE_RESPONSE_INVALID"
CONFIG_INVALID = "CONFIG_INVALID"


@dataclass(frozen=True)
class CentralProbeResult:
    reachable: bool
    accepted: bool
    http_status: int | None = None
    error_code: str = ""


def _safe_status(value) -> int | None:
    if type(value) is int and 100 <= value <= 599:
        return value
    return None


def _reject_duplicate_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


def _is_exact_success_body(raw_body: bytes) -> bool:
    if not raw_body or len(raw_body) > _MAX_PROBE_BODY_BYTES:
        return False
    try:
        parsed = json.loads(raw_body, object_pairs_hook=_reject_duplicate_members)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    return type(parsed) is dict and parsed == _SUCCESS_BODY and type(parsed.get("status")) is str


def _usable_credentials(credentials) -> bool:
    if not isinstance(credentials, NodeCredentials):
        return False
    if type(credentials.central_url) is not str or type(credentials.secret) is not str:
        return False
    scheme = urlsplit(credentials.central_url).scheme
    if scheme == "https":
        return True
    return scheme == "http" and credentials.allow_insecure_http is True


def probe_central(credentials: NodeCredentials, *, opener=None) -> CentralProbeResult:
    if not _usable_credentials(credentials):
        return CentralProbeResult(reachable=False, accepted=False, error_code=CONFIG_INVALID)

    request = urllib.request.Request(
        credentials.central_url.rstrip("/") + _PROBE_PATH,
        method="GET",
        headers={
            "Authorization": f"Bearer {credentials.secret}",
            "X-LinuxLab-Node-Id": str(credentials.node_id),
        },
    )
    active_opener = opener if opener is not None else _TRANSPORT_OPENER

    try:
        with active_opener.open(request, timeout=_PROBE_TIMEOUT_SECONDS) as response:
            status = _safe_status(getattr(response, "status", None))
            raw_body = response.read(_MAX_PROBE_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # Reachable: Central answered. Only the status code crosses this
        # boundary -- never the response body, headers or str(exc).
        status = _safe_status(exc.code)
        code = AUTH_REJECTED if status in (401, 403) else PROBE_RESPONSE_INVALID
        return CentralProbeResult(
            reachable=True, accepted=False, http_status=status, error_code=code
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        # DNS/TLS/timeout/socket failure: fixed safe outcome, never str(exc)
        # (which can embed the host, URL or Authorization material).
        return CentralProbeResult(reachable=False, accepted=False, error_code=PROBE_UNREACHABLE)

    if status != 200 or not isinstance(raw_body, bytes) or not _is_exact_success_body(raw_body):
        return CentralProbeResult(
            reachable=True, accepted=False, http_status=status, error_code=PROBE_RESPONSE_INVALID
        )
    return CentralProbeResult(reachable=True, accepted=True, http_status=200)
