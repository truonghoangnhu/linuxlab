"""Task 15 Task 11 corrective v11 (design spec Section 26/27, Section
32 items 11-15): artifact coverage the v10 review identified as still
registered too late or not at all -- a tampered `.llpkg`, every restart/
restored Gunicorn log, and dump-file producers that can partially create
their output before raising. Real ephemeral PostgreSQL/Gunicorn are used
throughout (never a simulated/boolean stand-in for "exists"), mirroring
scripts/tests/test_release_gate_corrective_v10_artifact_ownership.py's
own house style.
"""

import pytest

from scripts.release_gate import gate_runner, scenarios
from scripts.release_gate.cleanup import run_cleanup_actions
from scripts.release_gate.database import drop_database
from scripts.release_gate.e2e_support import author_and_package, dual_deployment

_CONTENT = {"prompt": "Which command lists files?", "options": [{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}]}


# --- RED test 32.11: tampered package live ownership -------------------------


def test_tampered_llpkg_is_ledger_owned_while_physically_live(tmp_path):
    ledger = gate_runner.ResourceLedger()

    with dual_deployment(tmp_path, start_node=False, resource_ledger=ledger) as deployment:
        _authored, package_path = author_and_package(
            deployment, prompt=_CONTENT["prompt"], options=_CONTENT["options"], correct_option_id="a"
        )
        tampered_path = scenarios._tamper_signature(package_path, deployment=deployment)
        assert tampered_path.exists() is True
        assert ledger.contains("temp_llpkg", str(tampered_path)) is True

    assert tampered_path.exists() is False
    assert ledger.contains("temp_llpkg", str(tampered_path)) is False


# --- RED test 32.12: restart/restored log ownership --------------------------


def test_restart_logs_are_ledger_owned_while_physically_live(tmp_path):
    ledger = gate_runner.ResourceLedger()

    with dual_deployment(tmp_path, resource_ledger=ledger) as deployment:
        scenarios.run_n5_node_restart(deployment)
        node_restarted_log_path = deployment.tmp_dir / "node-restarted.log"
        assert node_restarted_log_path.exists() is True
        assert ledger.contains("raw_runtime_log", str(node_restarted_log_path)) is True

        scenarios.run_n4_central_unavailable(deployment)
        central_restarted_log_path = deployment.tmp_dir / "central-restarted.log"
        assert central_restarted_log_path.exists() is True
        assert ledger.contains("raw_runtime_log", str(central_restarted_log_path)) is True

    assert node_restarted_log_path.exists() is False
    assert ledger.contains("raw_runtime_log", str(node_restarted_log_path)) is False
    assert central_restarted_log_path.exists() is False
    assert ledger.contains("raw_runtime_log", str(central_restarted_log_path)) is False


def test_restored_logs_are_ledger_owned_while_physically_live(tmp_path):
    ledger = gate_runner.ResourceLedger()

    with dual_deployment(tmp_path, resource_ledger=ledger) as deployment:
        try:
            detail = scenarios.run_backup_restore_smoke(deployment)
            restored_central_log_path = deployment.tmp_dir / "restored-central.log"
            restored_node_log_path = deployment.tmp_dir / "restored-node.log"
            assert restored_central_log_path.exists() is True
            assert restored_node_log_path.exists() is True
            assert ledger.contains("raw_runtime_log", str(restored_central_log_path)) is True
            assert ledger.contains("raw_runtime_log", str(restored_node_log_path)) is True
        finally:
            # Task 15 Task 11 corrective v8, Section 15/16: restore-
            # database disposal is normally gate_runner._run_one_
            # scenario()'s own CLEANUP-stage responsibility -- this test
            # calls the scenario function directly, so it owns that
            # lifecycle itself here.
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

    assert detail["central_business_state_restored"] is True
    assert restored_central_log_path.exists() is False
    assert restored_node_log_path.exists() is False
    assert ledger.contains("raw_runtime_log", str(restored_central_log_path)) is False
    assert ledger.contains("raw_runtime_log", str(restored_node_log_path)) is False


# --- RED test 32.13: dump reservation before producer ------------------------


def test_dump_reservation_exists_before_producer_starts(tmp_path, monkeypatch):
    ledger = gate_runner.ResourceLedger()
    observed_already_owned = {}
    real_dump_database = scenarios.dump_database

    def _observing_dump_database(db_admin, gate_names, member, dump_path):
        observed_already_owned[member] = ledger.contains("temp_dump", str(dump_path))
        return real_dump_database(db_admin, gate_names, member, dump_path)

    monkeypatch.setattr(scenarios, "dump_database", _observing_dump_database)

    with dual_deployment(tmp_path, resource_ledger=ledger) as deployment:
        try:
            scenarios.run_backup_restore_smoke(deployment)
        finally:
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

    assert observed_already_owned.get("central") is True
    assert observed_already_owned.get("node") is True


# --- RED test 32.14: partial dump failure ------------------------------------


def test_partial_dump_output_is_already_ledger_owned_before_it_can_be_cleaned_up(tmp_path, monkeypatch):
    ledger = gate_runner.ResourceLedger()
    observed = {}
    real_dump_database = scenarios.dump_database

    def _partial_then_fail_dump_database(db_admin, gate_names, member, dump_path):
        if member == "node":
            # Simulates pg_dump having already opened/partially written
            # its output file before failing -- the ledger reservation
            # must already cover this exact path at this point.
            dump_path.write_bytes(b"partial-dump-bytes-before-crash")
            observed["node_dump_already_owned"] = ledger.contains("temp_dump", str(dump_path))
            observed["node_dump_path_exists"] = dump_path.exists()
            raise RuntimeError("simulated pg_dump crash after partial write")
        return real_dump_database(db_admin, gate_names, member, dump_path)

    monkeypatch.setattr(scenarios, "dump_database", _partial_then_fail_dump_database)

    with pytest.raises(RuntimeError, match="simulated pg_dump crash"), dual_deployment(
        tmp_path, resource_ledger=ledger
    ) as deployment:
        scenarios.run_backup_restore_smoke(deployment)

    assert observed["node_dump_already_owned"] is True
    assert observed["node_dump_path_exists"] is True
    # Workspace teardown (dual_deployment's own `finally`) removed the
    # entire workspace -- including the partial dump -- and structural
    # reconciliation released it, since its own locator is now verified
    # absent.
    node_dump_path = deployment.tmp_dir / "node.dump"
    assert node_dump_path.exists() is False
    assert ledger.contains("temp_dump", str(node_dump_path)) is False
    assert ledger.is_empty()


# --- RED test 32.15: producer fails before creating output -------------------


def test_reserved_dump_path_never_created_is_released_after_verified_absence(tmp_path, monkeypatch):
    ledger = gate_runner.ResourceLedger()
    observed = {}

    def _fails_before_create_dump_database(db_admin, gate_names, member, dump_path):
        if member == "central":
            observed["central_dump_already_owned"] = ledger.contains("temp_dump", str(dump_path))
            observed["central_dump_path_exists"] = dump_path.exists()
            raise RuntimeError("simulated pg_dump failure before any output was written")
        raise AssertionError("node dump_database must never be reached")

    monkeypatch.setattr(scenarios, "dump_database", _fails_before_create_dump_database)

    with pytest.raises(RuntimeError, match="simulated pg_dump failure"), dual_deployment(
        tmp_path, resource_ledger=ledger
    ) as deployment:
        scenarios.run_backup_restore_smoke(deployment)

    assert observed["central_dump_already_owned"] is True
    assert observed["central_dump_path_exists"] is False
    central_dump_path = deployment.tmp_dir / "central.dump"
    assert ledger.contains("temp_dump", str(central_dump_path)) is False
    assert ledger.is_empty()
