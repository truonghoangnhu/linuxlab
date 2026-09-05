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
from scripts.release_gate.cleanup import run_cleanup_actions
from scripts.release_gate.database import GateDatabaseNames, drop_database
from scripts.release_gate.e2e_support import db_admin_from_env, dual_deployment


def test_central_and_node_backup_restore_smoke(tmp_path):
    with dual_deployment(tmp_path) as deployment:
        try:
            detail = scenarios.run_backup_restore_smoke(deployment)
        finally:
            # Task 15 Task 11 corrective v8, Section 15/16: restore-
            # database disposal is normally gate_runner._run_one_
            # scenario()'s own CLEANUP-stage responsibility, but this
            # test calls the scenario function directly (bypassing the
            # runner entirely) to test it in isolation -- so it is
            # responsible for its own full resource lifecycle here.
            run_cleanup_actions(
                [
                    (
                        "drop_restore_node_database",
                        lambda: drop_database(deployment.db_admin, deployment.gate_names, "restore_node"),
                    ),
                    (
                        "drop_restore_central_database",
                        lambda: drop_database(deployment.db_admin, deployment.gate_names, "restore_central"),
                    ),
                ]
            )

    # Task 15 Task 11 corrective v3, Section 13: backup_restore's
    # mandatory typed assertion set.
    assert detail["central_business_state_restored"] is True
    assert detail["node_business_state_restored"] is True
    assert detail["pending_outbox_restored"] is True
    assert detail["restored_sync_can_resume"] is True


def _new_state_and_run_id():
    return gate_runner.RunState(), "r" + uuid.uuid4().hex[:10]


def _cleanup_leaked_restore_db(outer_run_id, member):
    """Drops a restore database directly (bypassing any patched
    drop_database) -- used to clean up real residue this test file
    deliberately leaks while proving the ledger notices it."""
    child_run_id = gate_runner._child_run_id(outer_run_id, "backup_restore_smoke")
    gate_names = GateDatabaseNames.for_run(child_run_id)
    from scripts.release_gate.database import drop_database as real_drop_database

    real_drop_database(db_admin_from_env(), gate_names, member)


# --- Task 15 Task 11 corrective v8, Section 9-16: restore Central/Node
# databases are ACTIVE resource-ownership-ledger entries, acquired the
# moment each is created (never inferred later from cleanup-exception
# names), and their disposal executes under the runner's own CLEANUP
# stage -- never as part of the scenario body itself.


def test_partial_restore_database_creation_is_tracked_even_when_the_second_create_fails(tmp_path):
    """restore_central is created successfully; restore_node creation
    then fails. The ledger must already own restore_central (acquired
    the instant it was created, before restore_node was even
    attempted), so cleanup can see and dispose of it. The subsequent
    cleanup drop of restore_central is ALSO forced to fail here, so the
    real database's continued existence is what proves the ledger
    genuinely tracked it -- not merely that cleanup happened to
    succeed."""
    state, outer_run_id = _new_state_and_run_id()
    secrets = gate_runner.SecretRegistry()

    original_create_database = scenarios.create_database

    def _flaky_create_database(db_admin, gate_names, member, *args, **kwargs):
        if member == "restore_node":
            raise RuntimeError("simulated restore-node create failure")
        return original_create_database(db_admin, gate_names, member, *args, **kwargs)

    original_drop_database = gate_runner.drop_database

    def _flaky_drop_database(db_admin, gate_names, member, *args, **kwargs):
        if member == "restore_central":
            raise RuntimeError("simulated restore-central drop failure")
        return original_drop_database(db_admin, gate_names, member, *args, **kwargs)

    try:
        with (
            patch.object(scenarios, "create_database", _flaky_create_database),
            patch.object(gate_runner, "drop_database", _flaky_drop_database),
        ):
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
        retained = state.resources.retained_execution_resources()
        assert any(ref.kind == gate_runner.ResourceKind.RESTORE_CENTRAL_DB for ref in retained)
        assert not any(ref.kind == gate_runner.ResourceKind.RESTORE_NODE_DB for ref in retained)
    finally:
        _cleanup_leaked_restore_db(outer_run_id, "restore_central")


def test_partial_restore_creation_with_successful_cleanup_leaves_no_retained_resource(tmp_path):
    """Converse of the above: restore_node creation still fails, but
    THIS time the cleanup drop of restore_central succeeds for real --
    the database must actually be gone, and the ledger must correctly
    show nothing retained."""
    state, outer_run_id = _new_state_and_run_id()
    secrets = gate_runner.SecretRegistry()

    original_create_database = scenarios.create_database

    def _flaky_create_database(db_admin, gate_names, member, *args, **kwargs):
        if member == "restore_node":
            raise RuntimeError("simulated restore-node create failure")
        return original_create_database(db_admin, gate_names, member, *args, **kwargs)

    with patch.object(scenarios, "create_database", _flaky_create_database):
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
    assert state.resources.retained_execution_resources() == ()


def test_restore_database_cleanup_failure_is_classified_under_cleanup_not_scenario(tmp_path):
    """Task 15 Task 11 corrective v8, Section 15/16: once the scenario's
    OWN semantic backup/restore assertions have all succeeded, a
    restore-database DROP failure is a CLEANUP concern -- the scenario
    itself must still report PASS (its own work genuinely succeeded),
    while cleanup_failure (not primary_failure) records the drop
    failure and the ledger keeps the residue retained. Forcing this
    drop to fail deliberately leaks the real `restore_central` database
    for the duration of the test -- cleaned up for real in `finally`,
    independent of assertion outcome."""
    state, outer_run_id = _new_state_and_run_id()
    secrets = gate_runner.SecretRegistry()

    original_drop_database = gate_runner.drop_database

    def _flaky_drop_database(db_admin, gate_names, member, *args, **kwargs):
        if member == "restore_central":
            raise RuntimeError("simulated restore-central drop failure")
        return original_drop_database(db_admin, gate_names, member, *args, **kwargs)

    try:
        with patch.object(gate_runner, "drop_database", _flaky_drop_database):
            result, _detail_value = gate_runner._run_one_scenario(
                state,
                outer_run_id,
                "backup_restore_smoke",
                scenarios.run_backup_restore_smoke,
                tmp_path,
                dual_deployment,
                secrets,
            )

        assert result.status == "PASS", "the scenario's own semantic work succeeded; only disposal failed"
        assert state.primary_failure is None
        assert state.cleanup_failure is not None
        retained = state.resources.retained_execution_resources()
        assert any(ref.kind == gate_runner.ResourceKind.RESTORE_CENTRAL_DB for ref in retained)
    finally:
        _cleanup_leaked_restore_db(outer_run_id, "restore_central")


def test_restore_database_cleanup_failure_never_overrides_an_earlier_scenario_primary_failure(tmp_path):
    """If the scenario's OWN semantic work fails, that primary failure
    remains authoritative even though restore-database disposal ALSO
    fails afterward -- the disposal failure is still recorded, just
    never as (or in place of) the primary failure."""
    state, outer_run_id = _new_state_and_run_id()
    secrets = gate_runner.SecretRegistry()

    def _broken_scenario(deployment):
        # Exercise the SAME acquisition path a real backup/restore run
        # would (so cleanup still has something real to dispose of),
        # then fail before returning.
        scenarios.create_database(deployment.db_admin, deployment.gate_names, "restore_central")
        ledger = getattr(deployment, "resource_ledger", None)
        if ledger is not None:
            ledger.acquire(
                gate_runner.ResourceKind.RESTORE_CENTRAL_DB, deployment.gate_names.restore_central
            )
        raise AssertionError("simulated scenario body failure")

    original_drop_database = gate_runner.drop_database

    def _flaky_drop_database(db_admin, gate_names, member, *args, **kwargs):
        if member == "restore_central":
            raise RuntimeError("simulated restore-central drop failure")
        return original_drop_database(db_admin, gate_names, member, *args, **kwargs)

    try:
        with patch.object(gate_runner, "drop_database", _flaky_drop_database):
            result, _detail_value = gate_runner._run_one_scenario(
                state, outer_run_id, "backup_restore_smoke", _broken_scenario, tmp_path, dual_deployment, secrets
            )

        assert result.status == "FAIL"
        assert state.primary_failure is not None
        assert state.primary_failure.category == "SCENARIO_FAILURE"
        assert state.cleanup_failure is not None
    finally:
        _cleanup_leaked_restore_db(outer_run_id, "restore_central")
