"""Task 15 Task 11 corrective v2, Section 18.5 ("Keep-on-failure test"):
a runner-level test combining the REAL gate_runner.run_release_gate()
orchestration with the REAL dual_deployment() (not fakes) -- one
RunContext, a controlled scenario failure with --keep-on-failure
requested -- proving that execution resources (the ephemeral Central/
Node databases and the workspace) are STILL fully destroyed, and that
only the sanitized evidence pair persists.
"""

import subprocess
from pathlib import Path

import psycopg
import pytest

from scripts.release_gate import gate_runner
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


def _any_gate_database_exists_for_run(db_admin, run_id):
    with psycopg.connect(
        host=db_admin.host,
        port=db_admin.port,
        user=db_admin.user,
        password=db_admin.password,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname LIKE %s", (f"linuxlab_gate_%_{run_id}%",)
        ).fetchone()
        return row is not None


def _working_tree_is_clean() -> bool:
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True)
    return result.stdout.strip() == ""


@pytest.mark.skipif(not _working_tree_is_clean(), reason="requires a clean working tree (real preflight)")
def test_keep_on_failure_integration_destroys_execution_resources_and_retains_only_sanitized_evidence(tmp_path):
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

    assert result.overall == "SCENARIO_FAILURE"
    assert result.exit_code == 4

    run_id = result.evidence.run["run_id"]
    db_admin = _admin_connection()

    # Execution resources: ALWAYS fully destroyed, --keep-on-failure or not.
    assert not _any_gate_database_exists_for_run(db_admin, run_id)
    # The run's own outer temp root (containing every scenario's
    # workspace) was removed unconditionally by run_release_gate() itself.
    assert not any(tmp_path.glob(f"phase1-release-{run_id}-*"))

    # Retention is recorded truthfully: the flag was requested, but
    # execution resources were never retained -- only the sanitized
    # evidence bundle persists.
    retention = result.evidence.retention
    assert retention["keep_on_failure_requested"] is True
    assert retention["execution_resources_retained"] is False
    assert retention["sanitized_debug_bundle_retained"] is True

    assert Path(result.json_path).exists()
    assert Path(result.markdown_path).exists()

    # Zero plaintext secrets anywhere in the written evidence.
    json_text = Path(result.json_path).read_text()
    markdown_text = Path(result.markdown_path).read_text()
    assert "BEGIN" not in json_text and "PRIVATE KEY" not in json_text
    assert "BEGIN" not in markdown_text and "PRIVATE KEY" not in markdown_text
