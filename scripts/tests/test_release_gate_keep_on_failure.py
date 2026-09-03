"""Task 15 Task 11: dual_deployment(keep_on_failure=...) -- local-
debug-only retention of ephemeral resources on a FAILED run.

Approved semantics:
- default (keep_on_failure=False, or a successful run regardless of
  the flag): full cleanup, exactly as already closed in Tasks 7-10 --
  workspace, private key, and both ephemeral databases all gone.
- keep_on_failure=True AND the scenario body raised: the ephemeral
  Central/Node databases and the workspace directory (containing the
  .llpkg, logs, etc.) are retained for inspection, but the private
  signing key is STILL deleted unconditionally -- keep-on-failure
  never permits retaining it.
"""

import psycopg
import pytest

from scripts.release_gate import e2e_support


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


def test_keep_on_failure_true_on_success_still_cleans_everything(tmp_path):
    with e2e_support.dual_deployment(tmp_path, start_node=False, keep_on_failure=True) as deployment:
        workspace = deployment.tmp_dir
        gate_names = deployment.gate_names
        db_admin = deployment.db_admin
        private_key_path = deployment.private_key_path
        assert workspace.exists()

    assert not workspace.exists()
    assert not private_key_path.exists()
    assert not _database_exists(db_admin, gate_names.central)
    assert not _database_exists(db_admin, gate_names.node)


def test_keep_on_failure_true_on_failure_retains_workspace_and_dbs_but_deletes_private_key(tmp_path):
    captured = {}
    with (
        pytest.raises(AssertionError, match="simulated scenario failure"),
        e2e_support.dual_deployment(tmp_path, start_node=False, keep_on_failure=True) as deployment,
    ):
        captured["workspace"] = deployment.tmp_dir
        captured["gate_names"] = deployment.gate_names
        captured["db_admin"] = deployment.db_admin
        captured["private_key_path"] = deployment.private_key_path
        captured["public_key_path"] = deployment.public_key_path
        raise AssertionError("simulated scenario failure")

    try:
        # Retained for local debugging:
        assert captured["workspace"].exists()
        assert captured["public_key_path"].exists()
        assert _database_exists(captured["db_admin"], captured["gate_names"].central)
        assert _database_exists(captured["db_admin"], captured["gate_names"].node)

        # Never retained, even under keep-on-failure:
        assert not captured["private_key_path"].exists()
    finally:
        # Test-side cleanup so this test doesn't itself leak the
        # deliberately-retained resources it just proved were kept.
        from scripts.release_gate.cleanup import remove_workspace
        from scripts.release_gate.database import drop_database

        remove_workspace(captured["workspace"])
        drop_database(captured["db_admin"], captured["gate_names"], "node")
        drop_database(captured["db_admin"], captured["gate_names"], "central")


def test_keep_on_failure_false_on_failure_cleans_everything_as_before(tmp_path):
    """Regression: explicit False on a failed run behaves exactly like
    the already-closed Tasks 7-10 default -- nothing is retained."""
    captured = {}
    with (
        pytest.raises(AssertionError, match="simulated scenario failure"),
        e2e_support.dual_deployment(tmp_path, start_node=False, keep_on_failure=False) as deployment,
    ):
        captured["workspace"] = deployment.tmp_dir
        captured["gate_names"] = deployment.gate_names
        captured["db_admin"] = deployment.db_admin
        raise AssertionError("simulated scenario failure")

    assert not captured["workspace"].exists()
    assert not _database_exists(captured["db_admin"], captured["gate_names"].central)
    assert not _database_exists(captured["db_admin"], captured["gate_names"].node)


def test_keep_on_failure_defaults_to_false(tmp_path):
    """dual_deployment() with no explicit keep_on_failure argument at
    all must behave exactly like Tasks 7-10's already-closed default --
    proven independently of the explicit-False test above, in case a
    future default-value change ever diverges the two."""
    captured = {}
    with (
        pytest.raises(AssertionError, match="simulated scenario failure"),
        e2e_support.dual_deployment(tmp_path, start_node=False) as deployment,
    ):
        captured["workspace"] = deployment.tmp_dir
        raise AssertionError("simulated scenario failure")

    assert not captured["workspace"].exists()
