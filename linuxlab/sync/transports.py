"""Task 14 SyncTransport implementations (T14-D01, T14-D02, T14-D29,
T14-D39); Corrective B Task 6 adds semantic HTTP failure classification
and a Retry-After parser.

HttpSyncTransport is the production transport: HTTPS-only (certificate
verification is `urllib.request`'s unconditional default; there is no
alternate code path that would fall back to plain HTTP), a Bearer
secret + Node ID sent as headers only (never in the request body or in
any log-safe repr), and strict, per-event ACK parsing -- a bare 2xx
response never implies every event was delivered. Redirects are never
followed automatically, so Authorization/Bearer credentials can never
be silently forwarded to a second origin; a 3xx response is classified
as a conservative transient failure instead.

FileSyncTransport is dev/test/reference only: it writes a deterministic
canonical batch file and always returns every event UNCONFIRMED, so it
can never itself cause a caller to infer DELIVERED.
"""

import json
import math
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .contracts import (
    AuthTransportError,
    BatchAck,
    DeliveryFailure,
    ProtocolTransportError,
    RateLimitedTransportError,
    RequestTooLargeError,
    SyncOutcome,
    TransientTransportError,
    TransportError,
    canonical_event_dict,
    parse_ack_results,
    serialize_sync_batch,
)

_EVENTS_PATH = "/api/v1/sync/events/"

_MAX_RETRY_AFTER_HEADER_LENGTH = 4096


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow an HTTP redirect automatically (Corrective B Task 6
    §10/§12): Authorization/Bearer credentials must never be silently
    forwarded to a second origin. Returning None from redirect_request()
    causes urllib to surface the 3xx response as an HTTPError instead of
    transparently re-requesting it at a different URL, so the caller
    classifies it (conservatively transient) rather than urllib crossing
    an origin boundary with credentials still attached."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Fix round 1 I1: a transport-OWNED opener, built once at import time but
# never installed as urllib.request's process-wide default (no
# urllib.request.install_opener() call). Importing this module must not
# mutate global urllib state, and HttpSyncTransport's redirect safety must
# not depend on -- or be overridable by -- whatever any other module
# later installs as the global opener. `.open()` is called directly on
# this object; the bare `urllib.request.urlopen()` function is
# deliberately never used for the real network call.
_TRANSPORT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def parse_retry_after(value: str | None, *, now: datetime) -> int | float | None:
    """Parse an HTTP Retry-After header value into a non-negative number
    of seconds, or None if it cannot be safely interpreted (Corrective B
    Task 6 §8). Tries integer delta-seconds first, then an HTTP-date via
    parsedate_to_datetime(), converted to aware UTC before subtracting
    `now`. Negative/expired results clamp to 0; malformed or nonfinite
    input returns None. No max_delay/retry-budget policy lives here --
    that is Task 8's concern. The raw header value is never retained in
    the result, and a header longer than 4096 characters is ignored
    outright rather than parsed."""
    if value is None:
        return None
    if len(value) > _MAX_RETRY_AFTER_HEADER_LENGTH:
        return None

    stripped = value.strip()
    if not stripped:
        return None

    # 1. Integer delta-seconds first. Fix round 1 I3: str.isdigit() alone
    # accepts non-ASCII "digit" characters (e.g. superscript "²", which
    # int() then rejects with an unhandled ValueError) AND non-ASCII
    # decimal digits (e.g. Arabic-Indic "١٢٣", which int() actually DOES
    # accept) -- ASCII decimal delta-seconds only. `digits.isascii() and
    # digits.isdecimal()` excludes both; the int() conversion is still
    # explicitly guarded for defense in depth even after that lexical
    # validation. Preserved as a Python int (arbitrary precision) even
    # for an excessively large value; never routed through float, which
    # could lose precision or overflow. Task 8 caps this to policy.
    sign = ""
    digits = stripped
    if stripped[0] in "+-":
        sign, digits = stripped[0], stripped[1:]
    if digits and digits.isascii() and digits.isdecimal():
        try:
            seconds = int(sign + digits)
        except ValueError:
            return None
        return max(seconds, 0)

    # 2. Then an HTTP-date.
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    if parsed is None:
        return None

    # 3. Convert to aware UTC safely -- parsedate_to_datetime() returns a
    # naive datetime when the header omits a timezone (Python >= 3.10),
    # never assume it is already UTC/local without making that explicit.
    parsed_utc = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    try:
        delta = (parsed_utc - now).total_seconds()
    except (TypeError, OverflowError):
        return None
    if not math.isfinite(delta):
        return None
    return max(0, delta)


def classify_http_failure(
    status: int, *, retry_after: str | None = None, now: datetime | None = None
) -> TransportError:
    """Map one concrete HTTP status to a semantic TransportError carrying
    explicit DeliveryFailure metadata (Corrective B Task 6 §7, approved
    spec §12). Returns the error; the caller raises it. No retry
    budget/max_delay/scheduling logic lives here -- that is Task 8's
    concern, and this classifier never inspects retry policy."""
    if status in (401, 403):
        return AuthTransportError(f"authentication rejected (HTTP {status})")
    if status == 400:
        return ProtocolTransportError(
            "request rejected (HTTP 400)",
            failure=DeliveryFailure(category=SyncOutcome.PERMANENT_REJECTION, http_status=400),
        )
    if status == 413:
        return RequestTooLargeError("request rejected (HTTP 413)")
    if status == 429:
        effective_now = now if now is not None else datetime.now(UTC)
        retry_after_seconds = parse_retry_after(retry_after, now=effective_now)
        return RateLimitedTransportError(
            "rate limited (HTTP 429)",
            failure=DeliveryFailure(
                category=SyncOutcome.RATE_LIMITED,
                http_status=429,
                retry_after_seconds=retry_after_seconds,
            ),
        )
    if status == 408:
        return TransientTransportError(f"request timeout (HTTP {status})")
    if 500 <= status <= 599:
        return TransientTransportError(f"server error (HTTP {status})")
    if 300 <= status <= 399:
        # Redirects are never followed (see _NoRedirectHandler); a 3xx
        # that still reaches here is classified conservatively transient.
        return TransientTransportError(f"unexpected redirect (HTTP {status})")
    # Any other unknown non-2xx status: conservative transient, never
    # "all 4xx are permanent".
    return TransientTransportError(f"unexpected HTTP status {status}")


class HttpSyncTransport:
    """Production `SyncTransport`. Constructed once per Node with its
    fixed Central URL, Node ID, and bearer secret."""

    def __init__(self, central_url, node_id, secret, *, allow_insecure_http=False, timeout=30):
        scheme = urlsplit(central_url).scheme
        if scheme not in ("http", "https"):
            raise ValueError(f"unsupported central_url scheme: {scheme!r}")
        if scheme == "http" and not allow_insecure_http:
            raise ValueError(
                "HttpSyncTransport requires https:// in production; pass "
                "allow_insecure_http=True only for explicit dev/test use"
            )
        self._events_url = central_url.rstrip("/") + _EVENTS_PATH
        self._node_id = node_id
        self._secret = secret
        self._timeout = timeout

    def __repr__(self):
        # Deliberately excludes the bearer secret from any log-safe repr.
        return f"HttpSyncTransport(node_id={self._node_id!r}, url={self._events_url!r})"

    def send_batch(self, envelopes):
        # The EXACT bytes claim_outbox_batch() budgeted against
        # (corrective, independent review finding #4) -- one shared
        # serializer, never a second differently-formatted encoding.
        body = serialize_sync_batch(envelopes)
        request = urllib.request.Request(
            self._events_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._secret}",
                "X-LinuxLab-Node-Id": str(self._node_id),
            },
        )

        try:
            with _TRANSPORT_OPENER.open(request, timeout=self._timeout) as response:
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            # Never str(exc)/the raw response body -- only the status code
            # and a bounded, safe Retry-After header value ever cross into
            # classify_http_failure() (Corrective B Task 6 §9/§13).
            retry_after = None
            if exc.headers is not None:
                retry_after = exc.headers.get("Retry-After")
            raise classify_http_failure(
                exc.code, retry_after=retry_after, now=datetime.now(UTC)
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # Fixed safe message: never str(exc), which could embed the
            # target host/URL or other network internals.
            raise TransientTransportError("network failure") from None

        return self._parse_batch_response(raw_body)

    @staticmethod
    def _parse_batch_response(raw_body):
        try:
            parsed = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Fix round 1 M3: never chain the raw JSONDecodeError as an
            # explicit cause -- json.JSONDecodeError.doc retains the
            # ENTIRE raw response document verbatim, so keeping it
            # reachable via __cause__ would let logging/error-tracking/
            # traceback rendering leak whatever Central (or an attacker
            # impersonating it) put in that body. Class and
            # TRANSIENT_FAILURE category are unchanged (Task-8-boundary
            # compatibility ruling); only the raw-evidence retention is
            # removed.
            raise TransientTransportError("malformed JSON response") from None

        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
            raise TransientTransportError("malformed batch response shape")

        # Conservative ambiguity handling (duplicated/conflicting
        # event_ids, a REJECTED entry with no code) lives in the one
        # shared parser so every transport gets it for free.
        return parse_ack_results(parsed["results"])


class FileSyncTransport:
    """Dev/test/reference-only `SyncTransport` (T14-D39): writes each
    batch as a deterministic canonical JSON file. Every event's ack is
    always UNCONFIRMED (absent from results) -- a successful file write
    is never, by itself, evidence of production delivery."""

    def __init__(self, directory):
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)

    def send_batch(self, envelopes):
        if envelopes:
            batch_id = uuid4()
            payload = {
                "batch_id": str(batch_id),
                "written_at": datetime.now(UTC).isoformat(),
                "events": [canonical_event_dict(envelope) for envelope in envelopes],
            }
            file_path = self._directory / f"{batch_id}.json"
            file_path.write_text(
                json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8"
            )
        return BatchAck(results=())
