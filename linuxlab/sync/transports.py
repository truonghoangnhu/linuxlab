"""Task 14 SyncTransport implementations (T14-D01, T14-D02, T14-D29,
T14-D39).

HttpSyncTransport is the production transport: HTTPS-only (certificate
verification is `urllib.request`'s unconditional default; there is no
alternate code path that would fall back to plain HTTP), a Bearer
secret + Node ID sent as headers only (never in the request body or in
any log-safe repr), and strict, per-event ACK parsing -- a bare 2xx
response never implies every event was delivered.

FileSyncTransport is dev/test/reference only: it writes a deterministic
canonical batch file and always returns every event UNCONFIRMED, so it
can never itself cause a caller to infer DELIVERED.
"""

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .contracts import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    AuthTransportError,
    BatchAck,
    EventAck,
    ProtocolTransportError,
    TransientTransportError,
    canonical_event_dict,
)

_EVENTS_PATH = "/api/v1/sync/events/"
_ACK_STATUSES = frozenset({ACCEPTED, DUPLICATE, REJECTED})


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
        body = json.dumps(
            {"events": [canonical_event_dict(envelope) for envelope in envelopes]}
        ).encode("utf-8")
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
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            self._raise_for_status(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientTransportError(str(exc)) from exc

        return self._parse_batch_response(raw_body)

    @staticmethod
    def _raise_for_status(status):
        if status in (401, 403):
            raise AuthTransportError(f"authentication rejected (HTTP {status})")
        if status in (400, 413):
            raise ProtocolTransportError(f"request rejected (HTTP {status})")
        # 5xx and any other non-2xx: always safe to treat as retryable
        # rather than inventing a new terminal failure mode.
        raise TransientTransportError(f"unexpected HTTP status {status}")

    @staticmethod
    def _parse_batch_response(raw_body):
        try:
            parsed = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TransientTransportError("malformed JSON response") from exc

        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
            raise TransientTransportError("malformed batch response shape")

        acks = []
        for item in parsed["results"]:
            if not isinstance(item, dict):
                continue
            status_value = item.get("status")
            if status_value not in _ACK_STATUSES:
                continue
            try:
                event_id = UUID(str(item.get("event_id")))
            except (ValueError, TypeError, AttributeError):
                continue
            acks.append(
                EventAck(
                    event_id=event_id,
                    status=status_value,
                    code=str(item.get("code") or ""),
                    reason=str(item.get("reason") or ""),
                )
            )
        return BatchAck(results=tuple(acks))


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
