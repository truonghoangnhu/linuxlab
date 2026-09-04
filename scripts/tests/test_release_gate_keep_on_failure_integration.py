"""Task 15 Task 11 corrective, Section 13 "Keep-on-failure integration":
a runner-level test combining the REAL gate_runner.run_release_gate()
orchestration with the REAL dual_deployment() (not fakes) -- one
RunContext, real owned Central/Node database identities and workspace,
a controlled scenario failure, DB/workspace retention, unconditional
private key deletion, retained identities recorded in evidence, and a
full secret scan of the written evidence -- followed by explicit test
cleanup of the deliberately-retained resources so this test does not
itself leak them.
"""

import subprocess
from pathlib import Path

import psycopg
import pytest

from scripts.release_gate import gate_runner
from scripts.release_gate.cleanup import remove_workspace
from scripts.release_gate.database import GateDatabaseNames, drop_database
from scripts.release_gate.environment import DatabaseAdminConnection

_OK_VALIDATION = {
    "clean_tree": True,
    "central_check": "ok",
    "node_check": "ok",
    "central_migration_drift": "none",
    "node_migration_drift": "none",
}


def _admin_connection():
    import os

    return DatabaseAdminConnection(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ.get("POSTGRES_USER", "linuxlab"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )


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


def _working_tree_is_clean() -> bool:
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True)
    return result.stdout.strip() == ""


@pytest.mark.skipif(not _working_tree_is_clean(), reason="requires a clean working tree (real preflight)")
def test_keep_on_failure_integration_retains_real_owned_resources_and_deletes_private_key(tmp_path):
    def _controlled_failure(deployment):
        raise AssertionError("integration test: controlled scenario failure")

    result = gate_runner.run_release_gate(
        repo_root=gate_runner.REPO_ROOT,
        check_platform=False,
        keep_on_failure=True,
        scenario_specs=(("integration_controlled_failure", _controlled_failure),),
        run_validation=lambda env: dict(_OK_VALIDATION),
        tmp_root=tmp_path,
    )

    entry = result.evidence.retained_resources.get("integration_controlled_failure")
    try:
        assert result.overall == "SCENARIO_FAILURE"
        assert result.exit_code == 4
        assert entry is not None

        db_admin = _admin_connection()
        central_db = entry["central_database"]
        node_db = entry["node_database"]
        workspace_path = Path(entry["workspace_path"])

        # Real owned resources genuinely retained.
        assert _database_exists(db_admin, central_db)
        assert _database_exists(db_admin, node_db)
        assert workspace_path.exists()

        # Private key STILL deleted unconditionally, even though the
        # rest of the workspace was retained.
        assert list(workspace_path.rglob("release-gate-signing.pem")) == []

        # Zero plaintext secrets anywhere in the written evidence.
        json_text = result.json_path.read_text()
        markdown_text = result.markdown_path.read_text()
        assert "BEGIN" not in json_text and "PRIVATE KEY" not in json_text
        assert "BEGIN" not in markdown_text and "PRIVATE KEY" not in markdown_text
    finally:
        # Explicit test cleanup of the deliberately-retained resources.
        if entry is not None:
            db_admin = _admin_connection()
            run_id = entry["central_database"].removeprefix("linuxlab_gate_central_")
            gate_names = GateDatabaseNames.for_run(run_id)
            remove_workspace(Path(entry["workspace_path"]))
            drop_database(db_admin, gate_names, "node")
            drop_database(db_admin, gate_names, "central")
