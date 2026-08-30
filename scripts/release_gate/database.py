"""Task 15 fail-closed ephemeral PostgreSQL lifecycle and recovery
primitives (T15-D06, T15-D19, T15-D23).

Every database name any function here touches is validated against a
narrow allow-list of gate-owned prefixes BEFORE the operation runs --
this module refuses to create, drop, dump, or restore into a name that
doesn't look like one of its own ephemeral databases, even if asked to.
It never targets `linuxlab_central_dev`/`linuxlab_node_dev`/
`linuxlab_test` or any other name outside its own prefixes.
"""

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

_GATE_NAME_PATTERN_BY_PREFIX = (
    "linuxlab_gate_central_",
    "linuxlab_gate_node_",
    "linuxlab_gate_restore_central_",
    "linuxlab_gate_restore_node_",
)


class DatabaseSafetyError(Exception):
    """Raised when a database name fails the ephemeral-name safety
    check. Maps to PRECHECK_FAILURE."""


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
        return cls(
            run_id=run_id,
            central=f"linuxlab_gate_central_{run_id}",
            node=f"linuxlab_gate_node_{run_id}",
            restore_central=f"linuxlab_gate_restore_central_{run_id}",
            restore_node=f"linuxlab_gate_restore_node_{run_id}",
        )

    def all_names(self):
        return (self.central, self.node, self.restore_central, self.restore_node)


def _connect_admin(db_admin, dbname="postgres"):
    return psycopg.connect(
        host=db_admin.host,
        port=db_admin.port,
        user=db_admin.user,
        password=db_admin.password,
        dbname=dbname,
        autocommit=True,
    )


def create_database(db_admin, name: str) -> None:
    assert_ephemeral_database_name(name)
    with _connect_admin(db_admin) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def drop_database(db_admin, name: str) -> None:
    """Idempotent: dropping a name that never existed is a safe no-op,
    since a release-gate cleanup pass may run after a partial/failed
    create."""
    assert_ephemeral_database_name(name)
    with _connect_admin(db_admin) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def run_migrations(profile_env: dict, *, repo_root=None) -> None:
    """Explicitly run `manage.py migrate` for the given profile/database
    environment (T15-D22: migration is an explicit release step, never
    application startup)."""
    repo_root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    child_env = {**os.environ, **profile_env}
    subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        cwd=str(repo_root),
        env=child_env,
        check=True,
        capture_output=True,
        text=True,
    )


def dump_database(db_admin, name: str, dump_path) -> None:
    assert_ephemeral_database_name(name)
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


def restore_database(db_admin, target_name: str, dump_path) -> None:
    assert_ephemeral_database_name(target_name)
    child_env = {**os.environ, "PGPASSWORD": db_admin.password}
    subprocess.run(
        [
            "pg_restore",
            "-h", db_admin.host,
            "-p", str(db_admin.port),
            "-U", db_admin.user,
            "--no-owner",
            "-d", target_name,
            str(dump_path),
        ],
        env=child_env,
        check=True,
        capture_output=True,
        text=True,
    )
