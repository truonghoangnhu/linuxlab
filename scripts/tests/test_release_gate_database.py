"""Task 15 fail-closed ephemeral PostgreSQL lifecycle (T15-D06, T15-D19,
T15-D23).

These tests use the real local PostgreSQL 18 service (CLAUDE.md: "Use
the existing local PostgreSQL installation") -- no mocked database
layer. Every test that creates a real ephemeral database cleans it up
in `finally`, independent of assertion outcome.
"""

import os
import uuid

import psycopg
import pytest

from scripts.release_gate.database import (
    DatabaseSafetyError,
    GateDatabaseNames,
    assert_ephemeral_database_name,
    create_database,
    drop_database,
    dump_database,
    restore_database,
)
from scripts.release_gate.environment import DatabaseAdminConnection


def _admin_connection():
    return DatabaseAdminConnection(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ.get("POSTGRES_USER", "linuxlab"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )


def _connect(db_admin, dbname):
    return psycopg.connect(
        host=db_admin.host,
        port=db_admin.port,
        user=db_admin.user,
        password=db_admin.password,
        dbname=dbname,
        autocommit=True,
    )


# --- naming / safety ------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "linuxlab_gate_central_rabc123",
        "linuxlab_gate_node_rabc123",
        "linuxlab_gate_restore_central_rabc123",
        "linuxlab_gate_restore_node_rabc123",
    ],
)
def test_assert_ephemeral_database_name_allows_gate_prefixes(name):
    assert_ephemeral_database_name(name)  # does not raise


@pytest.mark.parametrize(
    "name",
    [
        "linuxlab_central_dev",
        "linuxlab_node_dev",
        "linuxlab_test",
        "postgres",
        "production",
        "some_other_db",
        "",
    ],
)
def test_assert_ephemeral_database_name_rejects_unsafe_names(name):
    with pytest.raises(DatabaseSafetyError):
        assert_ephemeral_database_name(name)


def test_gate_database_names_for_run_produces_all_four_prefixes():
    names = GateDatabaseNames.for_run("rabc123")

    assert names.central == "linuxlab_gate_central_rabc123"
    assert names.node == "linuxlab_gate_node_rabc123"
    assert names.restore_central == "linuxlab_gate_restore_central_rabc123"
    assert names.restore_node == "linuxlab_gate_restore_node_rabc123"
    for name in names.all_names():
        assert_ephemeral_database_name(name)  # every produced name is self-consistent


def test_create_database_refuses_unsafe_name():
    with pytest.raises(DatabaseSafetyError):
        create_database(_admin_connection(), "linuxlab_central_dev")


def test_drop_database_refuses_unsafe_name():
    with pytest.raises(DatabaseSafetyError):
        drop_database(_admin_connection(), "linuxlab_test")


# --- real PostgreSQL create/drop --------------------------------------------


def test_create_and_drop_real_ephemeral_database():
    db_admin = _admin_connection()
    run_id = "r" + uuid.uuid4().hex[:10]
    name = GateDatabaseNames.for_run(run_id).central

    create_database(db_admin, name)
    try:
        with _connect(db_admin, name) as conn:
            conn.execute("SELECT 1")  # connects successfully -- DB really exists
    finally:
        drop_database(db_admin, name)

    with _connect(db_admin, "postgres") as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
    assert exists is None


def test_drop_database_is_idempotent_for_a_name_that_never_existed():
    db_admin = _admin_connection()
    run_id = "r" + uuid.uuid4().hex[:10]
    name = GateDatabaseNames.for_run(run_id).central

    drop_database(db_admin, name)  # does not raise even though it never existed


# --- dump / restore ----------------------------------------------------


def test_dump_and_restore_preserves_a_row(tmp_path):
    db_admin = _admin_connection()
    run_id = "r" + uuid.uuid4().hex[:10]
    names = GateDatabaseNames.for_run(run_id)

    create_database(db_admin, names.central)
    try:
        with _connect(db_admin, names.central) as conn:
            conn.execute("CREATE TABLE gate_probe (id serial primary key, label text)")
            conn.execute("INSERT INTO gate_probe (label) VALUES ('gate-dump-restore-proof')")

        dump_path = tmp_path / "central.dump"
        dump_database(db_admin, names.central, dump_path)
        assert dump_path.exists()
        assert dump_path.stat().st_size > 0

        create_database(db_admin, names.restore_central)
        try:
            restore_database(db_admin, names.restore_central, dump_path)

            with _connect(db_admin, names.restore_central) as conn:
                row = conn.execute("SELECT label FROM gate_probe").fetchone()
            assert row == ("gate-dump-restore-proof",)
        finally:
            drop_database(db_admin, names.restore_central)
    finally:
        drop_database(db_admin, names.central)


def test_dump_database_refuses_unsafe_source_name(tmp_path):
    with pytest.raises(DatabaseSafetyError):
        dump_database(_admin_connection(), "linuxlab_central_dev", tmp_path / "dump.bin")


def test_restore_database_refuses_unsafe_target_name(tmp_path):
    dump_path = tmp_path / "dump.bin"
    dump_path.write_bytes(b"not-a-real-dump")

    with pytest.raises(DatabaseSafetyError):
        restore_database(_admin_connection(), "linuxlab_test", dump_path)
