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
"""

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

from .environment import assert_valid_run_id

_GATE_NAME_PATTERN_BY_PREFIX = (
    "linuxlab_gate_central_",
    "linuxlab_gate_node_",
    "linuxlab_gate_restore_central_",
    "linuxlab_gate_restore_node_",
)

_MEMBERS = ("central", "node", "restore_central", "restore_node")


class DatabaseSafetyError(Exception):
    """Raised when a database name fails the ephemeral-name safety
    check, or when a caller attempts to operate outside the
    GateDatabaseNames + member-selector API surface. Maps to
    PRECHECK_FAILURE."""


def assert_ephemeral_database_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise DatabaseSafetyError("database name must be a non-empty string")
    for prefix in _GATE_NAME_PATTERN_BY_PREFIX:
        if name.startswith(prefix) and len(name) > len(prefix):
            return
    raise DatabaseSafetyError(
        f"refusing to operate on non-ephemeral-looking database name {name!r}"
    )


@dataclass(frozen=True)
class GateDatabaseNames:
    run_id: str
    central: str
    node: str
    restore_central: str
    restore_node: str

    @classmethod
    def for_run(cls, run_id: str) -> "GateDatabaseNames":
        assert_valid_run_id(run_id)
        return cls(
            run_id=run_id,
            central=f"linuxlab_gate_central_{run_id}",
            node=f"linuxlab_gate_node_{run_id}",
            restore_central=f"linuxlab_gate_restore_central_{run_id}",
            restore_node=f"linuxlab_gate_restore_node_{run_id}",
        )

    def all_names(self):
        return (self.central, self.node, self.restore_central, self.restore_node)


def _resolve_owned_name(gate_names, member: str) -> str:
    """The ONLY way any function below learns which database name to
    operate on. `gate_names` must be a real GateDatabaseNames (never a
    bare string -- a caller cannot pass an arbitrary name at all), and
    `member` must be one of the four known roles. The resolved name is
    re-validated for shape as defense in depth before it ever reaches
    a subprocess/connection."""
    if not isinstance(gate_names, GateDatabaseNames):
        raise DatabaseSafetyError(
            "database operations require a real GateDatabaseNames for the "
            "current run, produced by GateDatabaseNames.for_run(run_id) -- "
            "a bare database name string is never accepted"
        )
    if member not in _MEMBERS:
        raise DatabaseSafetyError(f"unknown database role {member!r}; must be one of {_MEMBERS}")
    name = getattr(gate_names, member)
    assert_ephemeral_database_name(name)
    return name


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


def drop_database(db_admin, gate_names, member: str) -> None:
    """Idempotent: dropping a name that never existed is a safe no-op,
    since a release-gate cleanup pass may run after a partial/failed
    create."""
    name = _resolve_owned_name(gate_names, member)
    with _connect_admin(db_admin) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


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
