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
    headers ok, body stalled/
      truncated/unreadable      -> reachable=True,  accepted=False, PROBE_RESPONSE_INVALID
    headers ok, Content-Length
      unsatisfied/untrusted     -> reachable=True,  accepted=False, PROBE_RESPONSE_INVALID
    DNS/TLS/timeout/network     -> reachable=False, accepted=False, PROBE_UNREACHABLE
    unusable credentials/URL    -> reachable=False, accepted=False, CONFIG_INVALID (no request)
"""

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .configuration import NodeCredentials
from .transports import _TRANSPORT_OPENER

_PROBE_PATH = "/api/v1/sync/probe/"
_PROBE_TIMEOUT_SECONDS = 30
_MAX_PROBE_BODY_BYTES = 4096
_MAX_CONTENT_LENGTH_DIGITS = len(str(_MAX_PROBE_BODY_BYTES))  # 4
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


def _declared_content_length(response):
    """Framing declared by the response headers (T11-I2): returns
    (trusted, declared) where `declared` is the single, well-formed
    Content-Length in bytes, or None for a close-delimited/chunked response
    with no Content-Length. `trusted` is False for framing that must not be
    relied on: more than one Content-Length (duplicate or conflicting), a
    non-decimal or negative value, a Content-Length alongside
    Transfer-Encoding, or a declared length beyond the Task-11 body limit.
    A response object without headers (only test doubles) is treated as
    close-delimited."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return True, None
    values = headers.get_all("Content-Length") or []
    if not values:
        return True, None
    if len(values) != 1 or headers.get_all("Transfer-Encoding"):
        return False, None
    # T11-I3: the header value is attacker-controlled remote input, and
    # int() on a long decimal string raises ValueError under Python's
    # integer string-conversion limit (sys.get_int_max_str_digits(), 4300
    # here) -- so bound the LENGTH first. The largest acceptable value,
    # 4096, has four digits: any longer digit string (including a
    # zero-padded one) cannot be accepted and is rejected without ever
    # asking int() to parse it. Only a 1-4 character ASCII-decimal string
    # is converted, which is total and bounded.
    text = values[0].strip()
    if not 1 <= len(text) <= _MAX_CONTENT_LENGTH_DIGITS:
        return False, None
    if not text.isascii() or not text.isdecimal():
        return False, None
    declared = int(text)
    if declared > _MAX_PROBE_BODY_BYTES:
        return False, None
    return True, declared


def _usable_credentials(credentials) -> bool:
    if not isinstance(credentials, NodeCredentials):
        return False
    if type(credentials.central_url) is not str or type(credentials.secret) is not str:
        return False
    scheme = urlsplit(credentials.central_url).scheme
    if scheme == "https":
        return True
    return scheme == "http" and credentials.allow_insecure_http is True


def _probe_url(central_url: str) -> str:
    """The dedicated probe endpoint as a URL PATH component of the
    configured Central base URL (T11-C1): scheme and host are kept, the
    probe path is joined onto the base PATH, and no query/fragment is ever
    carried -- so the request can never land on an unrelated path such as
    /health/ with the probe path swallowed into a query string. (The
    configuration authority already rejects a query-bearing base URL;
    this is the structural construction, not a second rule.)"""
    parts = urlsplit(central_url)
    path = parts.path.rstrip("/") + _PROBE_PATH
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def probe_central(credentials: NodeCredentials, *, opener=None) -> CentralProbeResult:
    if not _usable_credentials(credentials):
        return CentralProbeResult(reachable=False, accepted=False, error_code=CONFIG_INVALID)

    request = urllib.request.Request(
        _probe_url(credentials.central_url),
        method="GET",
        headers={
            "Authorization": f"Bearer {credentials.secret}",
            "X-LinuxLab-Node-Id": str(credentials.node_id),
        },
    )
    active_opener = opener if opener is not None else _TRANSPORT_OPENER

    # Boundary 1 (T11-I1): transport / open-response failure. Only here can
    # Central be "unreachable" -- DNS, TLS, connection, timeout BEFORE any
    # response headers arrive.
    try:
        response = active_opener.open(request, timeout=_PROBE_TIMEOUT_SECONDS)
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

    # Boundary 2 (T11-I1): valid response headers were received, so HTTP
    # reachability is established and the status is known. A stalled,
    # truncated, malformed-framing or otherwise unreadable BODY can only
    # mean "reachable but not accepted" -- never unreachable, never an
    # escaping exception. Only the fixed classification crosses here.
    status = _safe_status(getattr(response, "status", None))
    try:
        with response:
            framing_trusted, declared_length = _declared_content_length(response)
            raw_body = response.read(_MAX_PROBE_BODY_BYTES + 1)
    except (http.client.HTTPException, TimeoutError, OSError):
        return CentralProbeResult(
            reachable=True, accepted=False, http_status=status, error_code=PROBE_RESPONSE_INVALID
        )

    # Framing completeness (T11-I2): http.client's bounded read(amt) returns
    # a SHORT byte sequence on premature EOF without raising IncompleteRead
    # when Content-Length is unsatisfied, so exception handling alone cannot
    # establish complete framing. The body is accepted only when the
    # declared Content-Length is trustworthy and EXACTLY equals the bytes
    # actually consumed by the bounded read (a declared length above the
    # body limit is untrusted outright, so the read stays bounded).
    if not framing_trusted or (declared_length is not None and len(raw_body) != declared_length):
        return CentralProbeResult(
            reachable=True, accepted=False, http_status=status, error_code=PROBE_RESPONSE_INVALID
        )

    if status != 200 or not isinstance(raw_body, bytes) or not _is_exact_success_body(raw_body):
        return CentralProbeResult(
            reachable=True, accepted=False, http_status=status, error_code=PROBE_RESPONSE_INVALID
        )
    return CentralProbeResult(reachable=True, accepted=True, http_status=200)
