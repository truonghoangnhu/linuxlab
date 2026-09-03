"""Task 15 Tasks 7-10 partial-setup cleanup final corrective, Important:
dual_deployment()'s protected cleanup scope previously began AFTER
workspace creation and both create_database() calls -- so a failure in
the SECOND create_database() call (Node), with the first (Central)
having already succeeded for real, propagated with no `finally` ever
running: the real Central database and the just-created workspace
directory were both orphaned, and the cleanup aggregator never ran at
all.

The protected scope must begin BEFORE any of workspace creation or
either create_database() call, so a failure at any point after setup
begins still triggers every registered, idempotent cleanup action.

Real ephemeral PostgreSQL throughout -- the Central database created
here is genuine, not a simulated/boolean stand-in.
"""

import psycopg
import pytest

from scripts.release_gate import e2e_support
from scripts.release_gate.database import create_database as real_create_database


def _database_exists(db_admin, name):
    with psycopg.connect(
        host=db_admin.host,
        port=db_admin.port,
        user=db_admin.user,
        password=db_admin.password,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        return row is not None


def test_node_db_creation_failure_after_real_central_db_creation_leaves_no_orphan(
    tmp_path, monkeypatch
):
    """The permanent regression proof for the partial-setup cleanup
    window: real Central DB creation succeeds, Node DB creation is
    injected to fail immediately after, the context manager must never
    yield, and every owned resource (real Central DB, workspace) must
    be gone afterward -- with the aggregator having genuinely attempted
    cleanup for BOTH members, not just the one that was ever created."""
    create_calls = []
    drop_calls = []
    captured = {}

    def _spy_create_database(db_admin, gate_names, member):
        create_calls.append(member)
        captured["gate_names"] = gate_names
        captured["db_admin"] = db_admin
        if member == "node":
            raise RuntimeError("simulated Node DB creation failure")
        return real_create_database(db_admin, gate_names, member)

    real_drop_database = e2e_support.drop_database

    def _spy_drop_database(db_admin, gate_names, member, **kwargs):
        drop_calls.append(member)
        return real_drop_database(db_admin, gate_names, member, **kwargs)

    monkeypatch.setattr(e2e_support, "create_database", _spy_create_database)
    monkeypatch.setattr(e2e_support, "drop_database", _spy_drop_database)

    yielded = False
    with (
        pytest.raises(RuntimeError, match="simulated Node DB creation failure"),
        e2e_support.dual_deployment(tmp_path, start_node=False),
    ):
        yielded = True

    # --- context manager must never yield ------------------------------------
    assert yielded is False

    # --- create_database was genuinely attempted for both, Central for real --
    assert create_calls == ["central", "node"]

    gate_names = captured["gate_names"]
    db_admin = captured["db_admin"]

    # --- later cleanup actions were still attempted for BOTH members, proving
    # the hard invariant: Node cleanup being a no-op (never created) must
    # never skip Central cleanup (which really existed) ------------------------
    assert set(drop_calls) == {"node", "central"}

    # --- Central DB absent after failure --------------------------------------
    assert not _database_exists(db_admin, gate_names.central)

    # --- Node DB absent after failure (never existed, but idempotently proven)
    assert not _database_exists(db_admin, gate_names.node)

    # --- deployment workspace absent -------------------------------------------
    leftover_workspaces = list(tmp_path.glob("gate-workspace-*"))
    assert leftover_workspaces == []

    # --- zero signing/package/log artifacts anywhere under tmp_path -----------
    # (no process was ever started -- the injected failure happens during
    # database creation, well before start_gunicorn() or key generation are
    # ever reached -- so this also structurally proves zero Gunicorn/
    # release-gate child processes could have leaked from this scenario)
    assert list(tmp_path.rglob("*.pem")) == []
    assert list(tmp_path.rglob("*.llpkg")) == []
    assert list(tmp_path.rglob("*.log")) == []


def test_central_db_creation_failure_still_triggers_workspace_cleanup(tmp_path, monkeypatch):
    """Symmetric case: the FIRST create_database() call itself fails --
    the workspace (already created before it) must still be removed,
    and no database cleanup call for either member should be required
    to raise, since neither was ever created."""
    create_calls = []
    drop_calls = []

    def _fail_on_central(db_admin, gate_names, member):
        create_calls.append(member)
        raise RuntimeError("simulated Central DB creation failure")

    real_drop_database = e2e_support.drop_database

    def _spy_drop_database(db_admin, gate_names, member, **kwargs):
        drop_calls.append(member)
        return real_drop_database(db_admin, gate_names, member, **kwargs)

    monkeypatch.setattr(e2e_support, "create_database", _fail_on_central)
    monkeypatch.setattr(e2e_support, "drop_database", _spy_drop_database)

    yielded = False
    with (
        pytest.raises(RuntimeError, match="simulated Central DB creation failure"),
        e2e_support.dual_deployment(tmp_path, start_node=False),
    ):
        yielded = True

    assert yielded is False
    assert create_calls == ["central"]
    assert set(drop_calls) == {"node", "central"}

    leftover_workspaces = list(tmp_path.glob("gate-workspace-*"))
    assert leftover_workspaces == []
