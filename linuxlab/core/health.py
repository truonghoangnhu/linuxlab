"""Task 15 liveness/readiness endpoints (T15-D15).

/health/ and /healthz share process liveness: no database queries or
external service/configuration inspection.

/ready/ and /readyz check database connectivity, migration-state compatibility
(read-only -- it never runs `migrate`), and that the active deployment
profile is one this codebase recognizes. It never mutates schema or
configuration, and the HTTP response body carries only a generic
`{"status": "ok"|"not_ready"}` -- no exception text, host, credential,
key path, or settings dump. Capability diagnostics are separate; local
readiness never depends on sync, keys, registration or Central connectivity.
"""

from dataclasses import dataclass

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


def check_database() -> bool:
    try:
        connections[DEFAULT_DB_ALIAS].ensure_connection()
    except Exception:  # noqa: BLE001 -- any DB failure means not_ready, never a leaked traceback
        return False
    return True


def check_schema_state() -> bool:
    """Read-only: migration_plan() only inspects state, it never calls
    MigrationExecutor.migrate()."""
    try:
        if not _check_profile() or not check_profile_configuration():
            return False
        executor = MigrationExecutor(connections[DEFAULT_DB_ALIAS])
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception:  # noqa: BLE001 -- any inspection failure means not_ready
        return False
    return not plan


def _check_profile():
    profile = getattr(settings, "LINUXLAB_PROFILE", None)
    return isinstance(profile, str) and profile in _VALID_PROFILES


def check_profile_configuration() -> bool:
    """The active profile's own required configuration is present --
    at minimum, its database name resolved to a non-empty value (a
    profile settings module that failed to load a required value would
    otherwise silently target an unnamed database)."""
    databases = getattr(settings, "DATABASES", None)
    if not isinstance(databases, dict):
        return False
    database = databases.get(DEFAULT_DB_ALIAS)
    if not isinstance(database, dict):
        return False
    name = database.get("NAME")
    return isinstance(name, str) and bool(name.strip())


def evaluate_readiness() -> ReadinessResult:
    # Each local check handles its expected failure modes, but
    # this outer guard is deliberate defense in depth: readiness must
    # degrade to not_ready rather than ever let an unexpected exception
    # (from a check itself, not just the thing it checks) escape into an
    # HTTP 500 with a traceback -- the response contract promises no
    # internal detail leaks, under any failure.
    try:
        if not _check_profile():
            return ReadinessResult(ready=False, reason_code="invalid_profile")
        if not check_profile_configuration():
            return ReadinessResult(ready=False, reason_code="missing_profile_configuration")
        if not check_database():
            return ReadinessResult(ready=False, reason_code="database_unavailable")
        if not check_schema_state():
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
