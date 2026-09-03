"""Task 15 fail-closed ephemeral PostgreSQL lifecycle and recovery
primitives (T15-D06, T15-D19, T15-D23).

Checkpoint corrective (finding #1): prefix matching alone is not
authorization. Every destructive/data-moving operation requires a real
`GateDatabaseNames` for the current run plus a role selector
("central"/"node"/"restore_central"/"restore_node") -- there is NO
public function here that accepts a bare database-name string at all.
A caller can only ever select which of the current run's own four
names to operate on; it can never supply an arbitrary name value, so a
foreign-run name, a production-like name, an embedded-dev-DB name, or
a bare prefix can never be authorized, even if it happens to match the
gate's naming pattern.

Checkpoint corrective 2 (finding #1): a bare `GateDatabaseNames`
instance is itself forgeable -- its stored member strings used to be
trusted as-is. `_resolve_owned_name()` now RECOMPUTES the canonical
name from the object's own `run_id` + role and rejects any object whose
stored field does not exactly match that recomputation, so a forged
`central`/`node`/`restore_central`/`restore_node` value (even a
production-like or embedded-dev-name one, even one that looks like a
perfectly valid gate name for some OTHER run) can never reach a
connection or subprocess. `GateDatabaseNames.for_run()` is the only
supported producer (`init=False` blocks the plain keyword
constructor); an `_owner_token` sentinel is checked as defense in
depth, but the canonical recomputation is the actual safety boundary
-- it holds even against an object built by bypassing the constructor
entirely via `object.__new__()`/`object.__setattr__()`.
"""

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql

from .environment import assert_valid_run_id

DEFAULT_CLEANUP_POLL_INTERVAL_SECONDS = 0.2
DEFAULT_CLEANUP_DEADLINE_SECONDS = 15.0

_GATE_NAME_PATTERN_BY_PREFIX = (
    "linuxlab_gate_central_",
    "linuxlab_gate_node_",
    "linuxlab_gate_restore_central_",
    "linuxlab_gate_restore_node_",
)

_MEMBERS = ("central", "node", "restore_central", "restore_node")

# Identifies an object as genuinely produced by GateDatabaseNames.for_run()
# in THIS process. Defense in depth only, per the module docstring above --
# never a substitute for the canonical-recomputation check.
_OWNER_TOKEN = object()


class DatabaseSafetyError(Exception):
    """Raised when a database name fails the ephemeral-name safety
    check, or when a caller attempts to operate outside the
    GateDatabaseNames + member-selector API surface. Maps to
    PRECHECK_FAILURE."""


class DatabaseCleanupError(Exception):
    """Raised by drop_database() when a database cannot be safely
    dropped: either its other active connections never drain within the
    bounded deadline, or it somehow still exists after a plain DROP
    DATABASE. Diagnostics are sanitized -- database name and remaining
    connection count only, never a connection string, credential, or
    query text. Maps to CLEANUP_FAILURE. Never raised for a
    pg_terminate_backend()-style privilege error, because drop_database()
    never calls that function (Task 15 Tasks 7-10 final cleanup
    corrective, following the same pattern already hardened for T15-D27
    fresh-migration cleanup in linuxlab/learning/tests/test_fresh_migration.py)."""


def assert_ephemeral_database_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise DatabaseSafetyError("database name must be a non-empty string")
    for prefix in _GATE_NAME_PATTERN_BY_PREFIX:
        if name.startswith(prefix) and len(name) > len(prefix):
            return
    raise DatabaseSafetyError(
        f"refusing to operate on non-ephemeral-looking database name {name!r}"
    )


def _canonical_database_name(run_id: str, role: str) -> str:
    """The single formula every gate database name is derived from.
    Re-run on every resolution (never trusted from a stored field) so a
    forged object can never substitute an arbitrary name for a given
    run_id/role pair."""
    assert_valid_run_id(run_id)
    if role not in _MEMBERS:
        raise DatabaseSafetyError(f"unknown database role {role!r}; must be one of {_MEMBERS}")
    return f"linuxlab_gate_{role}_{run_id}"


@dataclass(frozen=True, init=False)
class GateDatabaseNames:
    run_id: str
    central: str
    node: str
    restore_central: str
    restore_node: str
    _owner_token: object = field(repr=False, compare=False)

    @classmethod
    def for_run(cls, run_id: str) -> "GateDatabaseNames":
        assert_valid_run_id(run_id)
        obj = object.__new__(cls)
        object.__setattr__(obj, "run_id", run_id)
        for role in _MEMBERS:
            object.__setattr__(obj, role, _canonical_database_name(run_id, role))
        object.__setattr__(obj, "_owner_token", _OWNER_TOKEN)
        return obj

    def all_names(self):
        return (self.central, self.node, self.restore_central, self.restore_node)


def _require_authentic_owner(gate_names) -> None:
    if not isinstance(gate_names, GateDatabaseNames):
        raise DatabaseSafetyError(
            "database operations require a real GateDatabaseNames for the "
            "current run, produced by GateDatabaseNames.for_run(run_id) -- "
            "a bare database name string is never accepted"
        )
    # Defense in depth: catches accidental misuse cheaply. NOT the
    # primary safety check -- a forged object built via
    # object.__new__()/object.__setattr__() could carry this exact
    # sentinel too, which is exactly why _resolve_owned_name() below
    # always recomputes the canonical name independently as well.
    if getattr(gate_names, "_owner_token", None) is not _OWNER_TOKEN:
        raise DatabaseSafetyError(
            "GateDatabaseNames must be produced by GateDatabaseNames.for_run(); "
            "an object lacking the authentic owner token is never accepted"
        )


def _resolve_owned_name(gate_names, member: str) -> str:
    """The ONLY way any function below learns which database name to
    operate on. Returns the name RECOMPUTED from `gate_names.run_id` +
    `member` -- never the caller-provided stored field unchecked -- and
    raises if that stored field disagrees with the recomputation, so a
    forged/mismatched owner can never reach a connection or
    subprocess."""
    _require_authentic_owner(gate_names)
    if member not in _MEMBERS:
        raise DatabaseSafetyError(f"unknown database role {member!r}; must be one of {_MEMBERS}")
    canonical = _canonical_database_name(gate_names.run_id, member)
    stored = getattr(gate_names, member)
    if stored != canonical:
        raise DatabaseSafetyError(
            f"GateDatabaseNames.{member} ({stored!r}) does not match the name "
            f"canonically derived from its own run_id ({canonical!r}) -- refusing "
            "to trust a forged/mismatched owner"
        )
    assert_ephemeral_database_name(canonical)
    return canonical


def _connect_admin(db_admin, dbname="postgres"):
    return psycopg.connect(
        host=db_admin.host,
        port=db_admin.port,
        user=db_admin.user,
        password=db_admin.password,
        dbname=dbname,
        autocommit=True,
    )


def create_database(db_admin, gate_names, member: str) -> None:
    name = _resolve_owned_name(gate_names, member)
    with _connect_admin(db_admin) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _database_exists(db_admin, name: str) -> bool:
    with _connect_admin(db_admin) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        return row is not None


def _other_backend_count(db_admin, name: str) -> int:
    with _connect_admin(db_admin) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        ).fetchone()
        return row[0]


def drop_database(
    db_admin,
    gate_names,
    member: str,
    *,
    poll_interval_seconds: float = DEFAULT_CLEANUP_POLL_INTERVAL_SECONDS,
    deadline_seconds: float = DEFAULT_CLEANUP_DEADLINE_SECONDS,
    on_wait=None,
) -> int:
    """Idempotent, privilege-safe cleanup (Task 15 Tasks 7-10 final
    cleanup corrective, following the same pattern already hardened for
    T15-D27 fresh-migration cleanup): never calls pg_terminate_backend(),
    pg_signal_backend(), or DROP DATABASE ... WITH (FORCE) -- all three
    require privileges the ordinary `linuxlab` application role is not
    guaranteed to hold over every connection that might transiently
    remain (e.g. an autovacuum worker it cannot terminate). Instead
    polls pg_stat_activity for this database's other active connections
    and waits, bounded by `deadline_seconds`, for them to close on their
    own -- exactly what a short-lived subprocess connection does when it
    exits -- before issuing a plain DROP DATABASE, then verifies the
    database is actually gone.

    Dropping a name that never existed (or that disappears between poll
    iterations) is a safe no-op, since a release-gate cleanup pass may
    run after a partial/failed create.

    Raises DatabaseCleanupError, with sanitized diagnostics (database
    name and remaining-connection count only), if the deadline is
    exceeded or the database still exists after DROP DATABASE.

    `on_wait(remaining)`, if given, is invoked once per poll iteration
    that observed at least one other active connection -- test-only
    observability, never required for correctness. Returns the number of
    such iterations, so a test can prove genuine waiting occurred rather
    than an immediate lucky drop."""
    name = _resolve_owned_name(gate_names, member)
    deadline = time.monotonic() + deadline_seconds
    waited_iterations = 0

    while True:
        if not _database_exists(db_admin, name):
            return waited_iterations

        remaining = _other_backend_count(db_admin, name)
        if remaining == 0:
            break

        waited_iterations += 1
        if on_wait is not None:
            on_wait(remaining)

        if time.monotonic() >= deadline:
            raise DatabaseCleanupError(
                f"cleanup deadline ({deadline_seconds}s) exceeded waiting for "
                f"{remaining} active connection(s) on {name!r} to close"
            )
        time.sleep(poll_interval_seconds)

    with _connect_admin(db_admin) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))

    if _database_exists(db_admin, name):
        raise DatabaseCleanupError(f"database {name!r} still exists after DROP DATABASE")

    return waited_iterations


def run_migrations(gate_names, member: str, profile_env: dict, *, repo_root=None) -> None:
    """Explicitly run `manage.py migrate` for the given profile
    environment (T15-D22: migration is an explicit release step, never
    application startup). POSTGRES_DB is always pinned to the resolved
    owned name LAST, so nothing in `profile_env` can override it."""
    name = _resolve_owned_name(gate_names, member)
    repo_root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    child_env = {**os.environ, **profile_env, "POSTGRES_DB": name}
    subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        cwd=str(repo_root),
        env=child_env,
        check=True,
        capture_output=True,
        text=True,
    )


def dump_database(db_admin, gate_names, member: str, dump_path) -> None:
    name = _resolve_owned_name(gate_names, member)
    child_env = {**os.environ, "PGPASSWORD": db_admin.password}
    subprocess.run(
        [
            "pg_dump",
            "-h", db_admin.host,
            "-p", str(db_admin.port),
            "-U", db_admin.user,
            "-Fc",
            "-f", str(dump_path),
            name,
        ],
        env=child_env,
        check=True,
        capture_output=True,
        text=True,
    )


def restore_database(db_admin, gate_names, member: str, dump_path) -> None:
    name = _resolve_owned_name(gate_names, member)
    child_env = {**os.environ, "PGPASSWORD": db_admin.password}
    subprocess.run(
        [
            "pg_restore",
            "-h", db_admin.host,
            "-p", str(db_admin.port),
            "-U", db_admin.user,
            "--no-owner",
            "-d", name,
            str(dump_path),
        ],
        env=child_env,
        check=True,
        capture_output=True,
        text=True,
    )
