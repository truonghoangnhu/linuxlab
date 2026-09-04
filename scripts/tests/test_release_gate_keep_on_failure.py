"""Task 15 Task 11 corrective v2, Section 3 ("Security decision:
execution resources are never retained"): dual_deployment() always
performs full cleanup -- ephemeral Central/Node databases, the
workspace directory, and the private signing key -- regardless of
whether the scenario body succeeded or failed. There is no
`keep_on_failure` retention branch any more: the ONLY place a failed
run's diagnostic content may persist is the sanitized evidence pair
gate_runner itself writes, never raw execution state. See
scripts/tests/test_release_gate_execution_resources_never_retained.py
for the runner-level real-infra proof of this same invariant.
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


def test_dual_deployment_cleans_everything_on_success(tmp_path):
    with e2e_support.dual_deployment(tmp_path, start_node=False) as deployment:
        workspace = deployment.tmp_dir
        gate_names = deployment.gate_names
        db_admin = deployment.db_admin
        private_key_path = deployment.private_key_path
        assert workspace.exists()

    assert not workspace.exists()
    assert not private_key_path.exists()
    assert not _database_exists(db_admin, gate_names.central)
    assert not _database_exists(db_admin, gate_names.node)


def test_dual_deployment_cleans_everything_on_failure_no_retention_branch_exists(tmp_path):
    """Task 15 Task 11 corrective v2, Section 3.1: on a FAILED run, the
    ephemeral databases, workspace, and private key are ALL still
    destroyed -- there is no flag that changes this."""
    captured = {}
    with (
        pytest.raises(AssertionError, match="simulated scenario failure"),
        e2e_support.dual_deployment(tmp_path, start_node=False) as deployment,
    ):
        captured["workspace"] = deployment.tmp_dir
        captured["gate_names"] = deployment.gate_names
        captured["db_admin"] = deployment.db_admin
        captured["private_key_path"] = deployment.private_key_path
        raise AssertionError("simulated scenario failure")

    assert not captured["workspace"].exists()
    assert not captured["private_key_path"].exists()
    assert not _database_exists(captured["db_admin"], captured["gate_names"].central)
    assert not _database_exists(captured["db_admin"], captured["gate_names"].node)


def test_dual_deployment_no_longer_accepts_a_keep_on_failure_argument(tmp_path):
    """Structural proof: retention is not merely defaulted off, the
    parameter itself does not exist any more -- there is no way for a
    caller to even request execution-resource retention."""
    with pytest.raises(TypeError):
        e2e_support.dual_deployment(tmp_path, start_node=False, keep_on_failure=True)
