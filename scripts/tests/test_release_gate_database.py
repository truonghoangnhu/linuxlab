"""Task 15 fail-closed ephemeral PostgreSQL lifecycle (T15-D06, T15-D19,
T15-D23).

Checkpoint corrective (finding #1): prefix matching alone is not
authorization. Every destructive/data-moving operation now requires a
real `GateDatabaseNames` for the current run plus a role selector
("central"/"node"/"restore_central"/"restore_node") -- there is no API
surface that accepts a bare database-name string at all, so a
foreign-run name, a production-like name, an embedded-dev-DB name, or
a bare prefix can never be authorized, even if it happens to match the
gate's naming pattern.

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
from scripts.release_gate.environment import DatabaseAdminConnection, PreflightError


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


def _run_id():
    return "r" + uuid.uuid4().hex[:10]


# --- naming shape / GateDatabaseNames ---------------------------------------


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


def test_gate_database_names_for_run_rejects_malformed_run_id():
    for bad_run_id in ("Has-Dash", "has space", "UPPER", "semi;colon", ""):
        with pytest.raises(PreflightError):
            GateDatabaseNames.for_run(bad_run_id)


def test_gate_database_names_for_different_runs_never_collide():
    a = GateDatabaseNames.for_run(_run_id())
    b = GateDatabaseNames.for_run(_run_id())

    assert set(a.all_names()).isdisjoint(set(b.all_names()))


# --- ownership: no bare-string API surface exists ---------------------------


def test_create_database_refuses_a_bare_string_target():
    with pytest.raises(DatabaseSafetyError):
        create_database(_admin_connection(), "linuxlab_gate_central_rabc123", "central")


def test_create_database_refuses_foreign_run_looking_string():
    with pytest.raises(DatabaseSafetyError):
        create_database(_admin_connection(), "linuxlab_gate_central_other_run", "central")


def test_create_database_refuses_production_like_string():
    with pytest.raises(DatabaseSafetyError):
        create_database(_admin_connection(), "linuxlab_gate_central_production", "central")


def test_create_database_refuses_embedded_dev_name_string():
    with pytest.raises(DatabaseSafetyError):
        create_database(
            _admin_connection(), "linuxlab_gate_restore_node_linuxlab_node_dev", "restore_node"
        )


def test_create_database_refuses_prefix_only_string():
    with pytest.raises(DatabaseSafetyError):
        create_database(_admin_connection(), "linuxlab_gate_central_", "central")


def test_create_database_refuses_unknown_member():
    names = GateDatabaseNames.for_run(_run_id())
    with pytest.raises(DatabaseSafetyError):
        create_database(_admin_connection(), names, "not_a_real_member")


@pytest.mark.parametrize(
    "op,args",
    [
        ("create_database", ()),
        ("drop_database", ()),
    ],
)
def test_no_connection_is_opened_for_a_rejected_bare_string(op, args, monkeypatch):
    import scripts.release_gate.database as database_module

    def _fail_if_called(*a, **kw):
        raise AssertionError(f"{op} must not open a DB connection for a rejected target")

    monkeypatch.setattr(database_module, "_connect_admin", _fail_if_called)
    func = getattr(database_module, op)

    with pytest.raises(DatabaseSafetyError):
        func(_admin_connection(), "linuxlab_gate_central_other_run", "central", *args)


def test_dump_database_refuses_bare_string_and_opens_no_subprocess(tmp_path, monkeypatch):
    import scripts.release_gate.database as database_module

    def _fail_if_called(*a, **kw):
        raise AssertionError("dump_database must not invoke pg_dump for a rejected target")

    monkeypatch.setattr(database_module.subprocess, "run", _fail_if_called)

    with pytest.raises(DatabaseSafetyError):
        dump_database(_admin_connection(), "linuxlab_central_dev", "central", tmp_path / "d.bin")


def test_restore_database_refuses_bare_string_and_opens_no_subprocess(tmp_path, monkeypatch):
    import scripts.release_gate.database as database_module

    def _fail_if_called(*a, **kw):
        raise AssertionError("restore_database must not invoke pg_restore for a rejected target")

    monkeypatch.setattr(database_module.subprocess, "run", _fail_if_called)
    dump_path = tmp_path / "dump.bin"
    dump_path.write_bytes(b"not-a-real-dump")

    with pytest.raises(DatabaseSafetyError):
        restore_database(_admin_connection(), "linuxlab_test", "restore_node", dump_path)


def test_run_migrations_pins_postgres_db_to_the_owned_name_ignoring_caller_override(monkeypatch):
    import scripts.release_gate.database as database_module

    names = GateDatabaseNames.for_run(_run_id())
    captured = {}

    def _fake_run(argv, cwd, env, check, capture_output, text):
        captured["env"] = env

    monkeypatch.setattr(database_module.subprocess, "run", _fake_run)

    database_module.run_migrations(
        names, "central", {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central", "POSTGRES_DB": "linuxlab_central_dev"}
    )

    assert captured["env"]["POSTGRES_DB"] == names.central


# --- real PostgreSQL create/drop --------------------------------------------


def test_create_and_drop_real_ephemeral_database():
    db_admin = _admin_connection()
    names = GateDatabaseNames.for_run(_run_id())

    create_database(db_admin, names, "central")
    try:
        with _connect(db_admin, names.central) as conn:
            conn.execute("SELECT 1")  # connects successfully -- DB really exists
    finally:
        drop_database(db_admin, names, "central")

    with _connect(db_admin, "postgres") as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (names.central,)
        ).fetchone()
    assert exists is None


def test_drop_database_is_idempotent_for_a_name_that_never_existed():
    db_admin = _admin_connection()
    names = GateDatabaseNames.for_run(_run_id())

    drop_database(db_admin, names, "central")  # does not raise even though it never existed


# --- corrective 2: canonical recomputation defeats forged owner objects ----


def _forge_gate_names(run_id="rabc123def456", **field_overrides):
    """Build a GateDatabaseNames-shaped object the way the checkpoint
    review did: bypass for_run() entirely via object.__new__() +
    object.__setattr__(), so even a caller who reaches directly into
    the dataclass's storage (not just its public constructor) is
    covered by the same rejection. Defaults are the CORRECT canonical
    names for `run_id` -- callers override exactly the field(s) they
    want to forge."""
    obj = object.__new__(GateDatabaseNames)
    canonical = {
        "run_id": run_id,
        "central": f"linuxlab_gate_central_{run_id}",
        "node": f"linuxlab_gate_node_{run_id}",
        "restore_central": f"linuxlab_gate_restore_central_{run_id}",
        "restore_node": f"linuxlab_gate_restore_node_{run_id}",
    }
    canonical.update(field_overrides)
    for key, value in canonical.items():
        object.__setattr__(obj, key, value)
    # Deliberately no _owner_token set at all -- this alone must be
    # sufficient for database_module._require_authentic_owner to reject
    # it, independent of whether the field values happen to be
    # canonical.
    return obj


def test_create_database_refuses_forged_object_with_production_like_central(monkeypatch):
    import scripts.release_gate.database as database_module

    monkeypatch.setattr(
        database_module,
        "_connect_admin",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    forged = _forge_gate_names(central="linuxlab_gate_central_production")

    with pytest.raises(DatabaseSafetyError):
        create_database(_admin_connection(), forged, "central")


def test_create_database_refuses_forged_object_with_embedded_dev_restore_node(monkeypatch):
    import scripts.release_gate.database as database_module

    monkeypatch.setattr(
        database_module,
        "_connect_admin",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    forged = _forge_gate_names(restore_node="linuxlab_gate_restore_node_linuxlab_node_dev")

    with pytest.raises(DatabaseSafetyError):
        create_database(_admin_connection(), forged, "restore_node")


def test_create_database_refuses_forged_object_with_foreign_run_looking_central(monkeypatch):
    """The `central` field looks like a perfectly valid gate name -- for
    a DIFFERENT run than the object's own run_id. Canonical
    recomputation from run_id must catch the mismatch."""
    import scripts.release_gate.database as database_module

    monkeypatch.setattr(
        database_module,
        "_connect_admin",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    forged = _forge_gate_names(
        run_id="rmyownrun000", central="linuxlab_gate_central_rsomeotherrun999"
    )

    with pytest.raises(DatabaseSafetyError):
        create_database(_admin_connection(), forged, "central")


@pytest.mark.parametrize(
    "op,field,value,member,extra_args",
    [
        ("create_database", "central", "linuxlab_gate_central_production", "central", ()),
        ("drop_database", "central", "linuxlab_gate_central_production", "central", ()),
    ],
)
def test_forged_owner_is_rejected_before_any_connection_across_all_writers(
    op, field, value, member, extra_args, monkeypatch
):
    import scripts.release_gate.database as database_module

    monkeypatch.setattr(
        database_module,
        "_connect_admin",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError(f"{op} must not connect")),
    )
    func = getattr(database_module, op)
    forged = _forge_gate_names(**{field: value})

    with pytest.raises(DatabaseSafetyError):
        func(_admin_connection(), forged, member, *extra_args)


def test_dump_database_refuses_forged_owner_and_opens_no_subprocess(tmp_path, monkeypatch):
    import scripts.release_gate.database as database_module

    monkeypatch.setattr(
        database_module.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not invoke pg_dump")),
    )
    forged = _forge_gate_names(central="linuxlab_gate_central_production")

    with pytest.raises(DatabaseSafetyError):
        dump_database(_admin_connection(), forged, "central", tmp_path / "d.bin")


def test_restore_database_refuses_forged_owner_and_opens_no_subprocess(tmp_path, monkeypatch):
    import scripts.release_gate.database as database_module

    monkeypatch.setattr(
        database_module.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not invoke pg_restore")),
    )
    dump_path = tmp_path / "dump.bin"
    dump_path.write_bytes(b"not-a-real-dump")
    forged = _forge_gate_names(restore_node="linuxlab_gate_restore_node_linuxlab_node_dev")

    with pytest.raises(DatabaseSafetyError):
        restore_database(_admin_connection(), forged, "restore_node", dump_path)


def test_run_migrations_refuses_forged_owner_and_opens_no_subprocess(monkeypatch):
    import scripts.release_gate.database as database_module

    monkeypatch.setattr(
        database_module.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not invoke migrate")),
    )
    forged = _forge_gate_names(central="linuxlab_gate_central_production")

    with pytest.raises(DatabaseSafetyError):
        database_module.run_migrations(
            forged, "central", {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}
        )


def test_create_database_refuses_a_perfectly_consistent_but_unauthenticated_forged_object(
    monkeypatch,
):
    """Even a forged object whose fields ARE canonically self-consistent
    (correctly derived from its own run_id) must still be rejected --
    it was never produced by GateDatabaseNames.for_run(), so it carries
    no authentic owner token."""
    import scripts.release_gate.database as database_module

    monkeypatch.setattr(
        database_module,
        "_connect_admin",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    forged = _forge_gate_names()  # all fields canonical for their own run_id

    with pytest.raises(DatabaseSafetyError):
        create_database(_admin_connection(), forged, "central")


def test_gate_database_names_public_constructor_is_not_available():
    """for_run() is the only supported producer -- direct keyword
    construction must not silently create a trusted-looking object."""
    with pytest.raises(TypeError):
        GateDatabaseNames(
            run_id="rabc123def456",
            central="linuxlab_gate_central_rabc123def456",
            node="linuxlab_gate_node_rabc123def456",
            restore_central="linuxlab_gate_restore_central_rabc123def456",
            restore_node="linuxlab_gate_restore_node_rabc123def456",
        )


def test_for_run_produced_names_are_internally_canonical():
    """The happy path -- an object legitimately produced by for_run()
    always has fields that already match their own canonical
    derivation, so _resolve_owned_name's consistency check never
    rejects genuine output."""
    names = GateDatabaseNames.for_run("ravalidrun001")

    assert names.central == "linuxlab_gate_central_ravalidrun001"
    assert names.restore_node == "linuxlab_gate_restore_node_ravalidrun001"


def test_create_database_for_one_run_never_touches_another_runs_names():
    db_admin = _admin_connection()
    mine = GateDatabaseNames.for_run(_run_id())
    other = GateDatabaseNames.for_run(_run_id())

    create_database(db_admin, mine, "central")
    try:
        with _connect(db_admin, "postgres") as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (other.central,)
            ).fetchone()
        assert exists is None
    finally:
        drop_database(db_admin, mine, "central")


# --- dump / restore ----------------------------------------------------


def test_dump_and_restore_preserves_a_row(tmp_path):
    db_admin = _admin_connection()
    names = GateDatabaseNames.for_run(_run_id())

    create_database(db_admin, names, "central")
    try:
        with _connect(db_admin, names.central) as conn:
            conn.execute("CREATE TABLE gate_probe (id serial primary key, label text)")
            conn.execute("INSERT INTO gate_probe (label) VALUES ('gate-dump-restore-proof')")

        dump_path = tmp_path / "central.dump"
        dump_database(db_admin, names, "central", dump_path)
        assert dump_path.exists()
        assert dump_path.stat().st_size > 0

        create_database(db_admin, names, "restore_central")
        try:
            restore_database(db_admin, names, "restore_central", dump_path)

            with _connect(db_admin, names.restore_central) as conn:
                row = conn.execute("SELECT label FROM gate_probe").fetchone()
            assert row == ("gate-dump-restore-proof",)
        finally:
            drop_database(db_admin, names, "restore_central")
    finally:
        drop_database(db_admin, names, "central")
