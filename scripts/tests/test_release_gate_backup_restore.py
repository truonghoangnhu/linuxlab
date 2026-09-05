"""Task 15 Task 10: Central + Node backup/restore smoke (T15-D16, T15-D23),
using real `pg_dump`/`pg_restore` against real ephemeral PostgreSQL
databases -- never a scheduled backup service, never a bundled secrets+DB
artifact. The scenario itself lives in
scripts.release_gate.scenarios.run_backup_restore_smoke() (Task 11
extraction) so this test and scripts/phase1_release_gate.py both
exercise the exact same real-infra logic. A successful `pg_restore`
alone is not treated as PASS: every assertion queries real
application-level state through the same release-gate action bridge
Tasks 7-9 use, against the restored databases specifically.
"""

import uuid
from unittest.mock import patch

from scripts.release_gate import gate_runner, scenarios
from scripts.release_gate.e2e_support import dual_deployment


def test_central_and_node_backup_restore_smoke(tmp_path):
    with dual_deployment(tmp_path) as deployment:
        detail = scenarios.run_backup_restore_smoke(deployment)

    # Task 15 Task 11 corrective v3, Section 13: backup_restore's
    # mandatory typed assertion set.
    assert detail["central_business_state_restored"] is True
    assert detail["node_business_state_restored"] is True
    assert detail["pending_outbox_restored"] is True
    assert detail["restored_sync_can_resume"] is True


def test_restore_database_cleanup_failure_is_tracked_as_a_retained_execution_resource(tmp_path):
    """Task 15 Task 11 corrective v7, Section 11/14: restore Central/Node
    databases are first-class resource-ownership-ledger entries, not
    inferred from cleanup-error names after the fact. Forces a REAL
    restore-database drop to fail (against real ephemeral PostgreSQL
    databases, through the real backup_restore_smoke scenario) and
    proves the gate-runner-level ledger reports it retained --
    cleanup_status failed, execution_resources_retained true, PASS
    impossible -- rather than silently converting it into an ordinary
    scenario result.

    Forcing this one drop to fail deliberately leaks the real
    `restore_central` database for the duration of the test (that is
    the whole point -- proving the ledger notices) -- cleaned up for
    real in `finally`, independent of assertion outcome."""
    from scripts.release_gate.database import GateDatabaseNames
    from scripts.release_gate.e2e_support import db_admin_from_env

    state = gate_runner.RunState()
    secrets = gate_runner.SecretRegistry()
    outer_run_id = "r" + uuid.uuid4().hex[:10]

    original_drop_database = scenarios.drop_database

    def _flaky_drop_database(db_admin, gate_names, member, *args, **kwargs):
        if member == "restore_central":
            raise RuntimeError("simulated restore-central drop failure")
        return original_drop_database(db_admin, gate_names, member, *args, **kwargs)

    try:
        with patch.object(scenarios, "drop_database", _flaky_drop_database):
            result, _detail_value = gate_runner._run_one_scenario(
                state,
                outer_run_id,
                "backup_restore_smoke",
                scenarios.run_backup_restore_smoke,
                tmp_path,
                dual_deployment,
                secrets,
            )

        assert result.status == "FAIL"
        assert state.cleanup_failure is not None or state.primary_failure is not None
        retained = state.resources.retained_execution_resources()
        assert any(ref.kind == gate_runner.ResourceKind.RESTORE_CENTRAL_DB for ref in retained)
        assert bool(retained) is True  # execution_resources_retained would derive True from this
    finally:
        child_run_id = gate_runner._child_run_id(outer_run_id, "backup_restore_smoke")
        original_drop_database(
            db_admin_from_env(), GateDatabaseNames.for_run(child_run_id), "restore_central"
        )
