"""Task 15 liveness/readiness endpoints (T15-D15).

/healthz is process liveness only: it never queries the database and
never inspects external services or configuration.

/readyz checks database connectivity, migration-state compatibility
(read-only -- it never runs `migrate`), and that the active deployment
profile is one this codebase recognizes. It never mutates schema or
configuration, and the HTTP response body carries only a generic
`{"status": "ok"|"not_ready"}` -- no exception text, host, credential,
key path, or settings dump. Detailed diagnosis belongs in sanitized
internal logs, not this response.
"""

import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse

# The only LINUXLAB_PROFILE values any settings module in this
# repository defines (central.py/node.py/test.py) -- an unrecognized
# value means the active deployment is misconfigured.
_VALID_PROFILES = frozenset({"central", "node", "test"})


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    reason_code: str | None = None


def _check_database():
    try:
        connections[DEFAULT_DB_ALIAS].ensure_connection()
    except Exception:  # noqa: BLE001 -- any DB failure means not_ready, never a leaked traceback
        return False
    return True


def _check_migrations_applied():
    """Read-only: migration_plan() only inspects state, it never calls
    MigrationExecutor.migrate()."""
    try:
        executor = MigrationExecutor(connections[DEFAULT_DB_ALIAS])
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception:  # noqa: BLE001 -- any inspection failure means not_ready
        return False
    return not plan


def _check_profile():
    return getattr(settings, "LINUXLAB_PROFILE", None) in _VALID_PROFILES


def _check_profile_configuration():
    """The active profile's own required configuration is present --
    at minimum, its database name resolved to a non-empty value (a
    profile settings module that failed to load a required value would
    otherwise silently target an unnamed database)."""
    return bool(settings.DATABASES.get(DEFAULT_DB_ALIAS, {}).get("NAME"))


def _check_sync_configuration():
    """Checkpoint corrective (finding #4): a Node instance with sync
    enabled but missing/malformed sync configuration must not be
    reported ready. Gated on LINUXLAB_CAN_RUN_SYNC_WORKER -- a fixed
    profile capability that is False on Central -- rather than a
    LINUXLAB_PROFILE string comparison, so Central can never be made to
    require Node-only settings. A sync-disabled Node (the default, per
    T14-D38) may omit this configuration entirely. This check reads
    settings only -- it never opens a network connection to Central."""
    if not (settings.LINUXLAB_CAN_RUN_SYNC_WORKER and settings.LINUXLAB_SYNC_ENABLED):
        return True

    node_id = settings.LINUXLAB_NODE_ID
    if not node_id:
        return False
    try:
        uuid.UUID(str(node_id))
    except (ValueError, AttributeError, TypeError):
        return False

    secret = settings.LINUXLAB_SYNC_SECRET
    if not secret or not str(secret).strip():
        return False

    central_url = settings.LINUXLAB_CENTRAL_URL
    if not central_url:
        return False
    parsed = urlparse(central_url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def evaluate_readiness() -> ReadinessResult:
    # Each _check_*() already catches its own expected failure modes, but
    # this outer guard is deliberate defense in depth: readiness must
    # degrade to not_ready rather than ever let an unexpected exception
    # (from a check itself, not just the thing it checks) escape into an
    # HTTP 500 with a traceback -- the response contract promises no
    # internal detail leaks, under any failure.
    try:
        if not _check_profile():
            return ReadinessResult(ready=False, reason_code="invalid_profile")
        if not _check_profile_configuration():
            return ReadinessResult(ready=False, reason_code="missing_profile_configuration")
        if not _check_sync_configuration():
            return ReadinessResult(ready=False, reason_code="invalid_sync_configuration")
        if not _check_database():
            return ReadinessResult(ready=False, reason_code="database_unavailable")
        if not _check_migrations_applied():
            return ReadinessResult(ready=False, reason_code="migrations_pending")
    except Exception:  # noqa: BLE001 -- see comment above
        return ReadinessResult(ready=False, reason_code="unexpected_error")
    return ReadinessResult(ready=True)


def healthz(request):
    del request
    return JsonResponse({"status": "ok"})


def readyz(request):
    del request
    result = evaluate_readiness()
    status = 200 if result.ready else 503
    body = {"status": "ok" if result.ready else "not_ready"}
    return JsonResponse(body, status=status)
