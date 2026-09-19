"""Pure sync parsing and lazy capability-scoped configuration; no deployment I/O."""

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

# One parser table for both environment construction and typed setting consumers.
_SPECS = {
    "LINUXLAB_SYNC_ENABLED": ("bool", False),
    "LINUXLAB_SYNC_ALLOW_INSECURE_HTTP": ("bool", False),
    "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE": ("bool", False),
    "LINUXLAB_SYNC_BACKOFF_BASE_SECONDS": ("duration", 5.0),
    "LINUXLAB_SYNC_BACKOFF_CAP_SECONDS": ("duration", 3600.0),
    "LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS": ("int", 10),
    "LINUXLAB_SYNC_RETRY_MAX_AGE": ("duration", 86400.0),
    "LINUXLAB_SYNC_BATCH_MAX_EVENTS": ("int", 100),
    "LINUXLAB_SYNC_BATCH_MAX_BYTES": ("int", 1000000),
    "LINUXLAB_SYNC_LEASE_SECONDS": ("int", 300),
}
_NAMES = frozenset(_SPECS) | {
    "LINUXLAB_NODE_ID",
    "LINUXLAB_SYNC_SECRET",
    "LINUXLAB_CENTRAL_URL",
    "LINUXLAB_CAN_RUN_SYNC_WORKER",
}
_CODES = frozenset(
    {"INVALID_VALUE", "INVALID_RANGE", "CAPABILITY_DISABLED", "DISABLED", "INVALID_PURPOSE"}
)


def _validate_error_fields(setting_name: str, code: str) -> None:
    if (
        type(setting_name) is not str
        or setting_name not in _NAMES
        or type(code) is not str
        or code not in _CODES
    ):
        raise ValueError("Invalid sync configuration error metadata.")


class SyncConfigurationError(ValueError):
    def __init__(self, setting_name: str, code: str):
        _validate_error_fields(setting_name, code)
        self.setting_name = setting_name
        self.code = code
        super().__init__(f"Invalid sync configuration: {setting_name} ({code}).")


@dataclass(frozen=True)
class DeferredSyncSettingError:
    setting_name: str
    code: str

    def __post_init__(self):
        _validate_error_fields(self.setting_name, self.code)

    def __bool__(self):
        raise SyncConfigurationError(self.setting_name, self.code)


@dataclass(frozen=True)
class SyncRetryPolicy:
    base_delay: float
    max_delay: float
    max_attempts: int
    max_retry_age: float


@dataclass(frozen=True)
class NodeCredentials:
    node_id: UUID
    secret: str = field(repr=False)
    central_url: str = field(repr=False)
    allow_insecure_http: bool = False


@dataclass(frozen=True)
class SyncConfiguration:
    enabled: bool
    require_central_probe: bool
    retry_policy: SyncRetryPolicy | None
    credentials: NodeCredentials | None = field(repr=False)
    batch_max_events: int | None = None
    batch_max_bytes: int | None = None
    lease_seconds: int | None = None


def _duration_fits(value: float) -> bool:
    try:
        delta = timedelta(seconds=value)
        datetime.now(UTC) + delta
    except (OverflowError, ValueError):
        return False
    return True


def parse_sync_setting(name: str, raw: object) -> bool | int | float | DeferredSyncSettingError:
    if type(name) is not str or name not in _SPECS:
        raise ValueError("Unknown sync setting.")
    kind, _default = _SPECS[name]
    if isinstance(raw, DeferredSyncSettingError):
        return DeferredSyncSettingError(name, raw.code)
    if kind == "bool":
        if type(raw) is bool:
            return raw
        if type(raw) is str and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true"
    elif kind == "int":
        if type(raw) is int or (type(raw) is str and re.fullmatch(r"[+]?[0-9]+", raw.strip())):
            try:
                value = int(raw)
            except ValueError:
                return DeferredSyncSettingError(name, "INVALID_VALUE")
            if 1 <= value <= 2147483647:
                return value
    elif type(raw) in (int, float, str):
        try:
            value = float(raw)
        except (ValueError, OverflowError):
            return DeferredSyncSettingError(name, "INVALID_VALUE")
        if math.isfinite(value) and value > 0 and _duration_fits(value):
            return value
    return DeferredSyncSettingError(name, "INVALID_VALUE")


def _settings(settings_obj):
    if settings_obj is None:
        from django.conf import settings

        return settings
    return settings_obj


def _read(source, name):
    result = parse_sync_setting(name, getattr(source, name, _SPECS[name][1]))
    if isinstance(result, DeferredSyncSettingError):
        raise SyncConfigurationError(result.setting_name, result.code)
    return result


def _retry_policy(source) -> SyncRetryPolicy:
    base = _read(source, "LINUXLAB_SYNC_BACKOFF_BASE_SECONDS")
    cap = _read(source, "LINUXLAB_SYNC_BACKOFF_CAP_SECONDS")
    attempts = _read(source, "LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS")
    age = _read(source, "LINUXLAB_SYNC_RETRY_MAX_AGE")
    if base >= cap:
        raise SyncConfigurationError("LINUXLAB_SYNC_BACKOFF_BASE_SECONDS", "INVALID_RANGE")
    # Preserve the existing singleton-400 algorithm, but reject its overflow.
    if not _duration_fits(cap * 2):
        raise SyncConfigurationError("LINUXLAB_SYNC_BACKOFF_CAP_SECONDS", "INVALID_RANGE")
    return SyncRetryPolicy(base, cap, attempts, age)


def load_node_identity(*, settings_obj=None) -> UUID:
    source = _settings(settings_obj)
    raw = getattr(source, "LINUXLAB_NODE_ID", None)
    try:
        if type(raw) not in (str, UUID):
            raise ValueError
        node_id = raw if type(raw) is UUID else UUID(raw)
    except ValueError:
        raise SyncConfigurationError("LINUXLAB_NODE_ID", "INVALID_VALUE") from None
    secret = getattr(source, "LINUXLAB_SYNC_SECRET", None)
    if type(secret) is not str or not secret.strip():
        raise SyncConfigurationError("LINUXLAB_SYNC_SECRET", "INVALID_VALUE")
    return node_id


def load_node_credentials(*, settings_obj=None) -> NodeCredentials:
    source = _settings(settings_obj)
    node_id = load_node_identity(settings_obj=source)
    url = getattr(source, "LINUXLAB_CENTRAL_URL", None)
    allow_http = _read(source, "LINUXLAB_SYNC_ALLOW_INSECURE_HTTP")
    try:
        if type(url) is not str or not url or any(char.isspace() or ord(char) < 32 for char in url):
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "#" in url
            # Corrective B Task 11 (T11-C1): a Central BASE URL carries no
            # query string -- endpoint paths are appended to it, and a
            # query would let an appended path be swallowed into the query.
            or "?" in url
            or parsed.query
            or (parsed.scheme == "http" and not allow_http)
        ):
            raise ValueError
        _ = parsed.port  # Validate a supplied port without resolving the hostname.
    except ValueError:
        raise SyncConfigurationError("LINUXLAB_CENTRAL_URL", "INVALID_VALUE") from None
    return NodeCredentials(node_id, source.LINUXLAB_SYNC_SECRET, url, allow_http)


def load_inbound_limits(*, settings_obj=None) -> tuple[int, int]:
    source = _settings(settings_obj)
    return (
        _read(source, "LINUXLAB_SYNC_BATCH_MAX_EVENTS"),
        _read(source, "LINUXLAB_SYNC_BATCH_MAX_BYTES"),
    )


def load_sync_configuration(
    *, settings_obj=None, purpose: Literal["retry", "worker", "probe", "release"] = "retry"
) -> SyncConfiguration:
    source = _settings(settings_obj)
    if purpose == "retry":
        return SyncConfiguration(False, False, _retry_policy(source), None)
    if purpose not in ("worker", "probe", "release"):
        raise SyncConfigurationError("LINUXLAB_SYNC_ENABLED", "INVALID_PURPOSE")
    if getattr(source, "LINUXLAB_CAN_RUN_SYNC_WORKER", False) is not True:
        if purpose == "release":
            return SyncConfiguration(False, False, None, None)
        raise SyncConfigurationError("LINUXLAB_CAN_RUN_SYNC_WORKER", "CAPABILITY_DISABLED")
    enabled = False if purpose == "probe" else _read(source, "LINUXLAB_SYNC_ENABLED")
    require_probe = (
        _read(source, "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE")
        if purpose == "release"
        else False
    )
    if purpose == "worker" and not enabled:
        raise SyncConfigurationError("LINUXLAB_SYNC_ENABLED", "DISABLED")
    if purpose == "release" and not enabled and not require_probe:
        return SyncConfiguration(False, False, None, None)
    policy = _retry_policy(source)
    credentials = load_node_credentials(settings_obj=source)
    if enabled:
        events, size = load_inbound_limits(settings_obj=source)
        lease = _read(source, "LINUXLAB_SYNC_LEASE_SECONDS")
        return SyncConfiguration(enabled, require_probe, policy, credentials, events, size, lease)
    return SyncConfiguration(enabled, require_probe, policy, credentials)
