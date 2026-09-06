"""Task 15 Task 11 corrective v10, Gap 4 (design spec Sections 15-20):
the generated `.llpkg` package file, backup/restore dump files, and
owned Gunicorn runtime log files were never individually represented in
the resource-ownership ledger -- only the enclosing workspace directory
was tracked, per the v8/v9 design comment "every generated artifact...
lives inside the workspace directory, so remove_workspace alone covers
all of them". v10 supersedes that specific claim: each of these three
artifact kinds now gets an individual live ledger entry while it
physically exists, even though physical disposal remains delegated to
the enclosing workspace's own cleanup.

These are the mandatory RED tests from the v10 design spec, Section 25,
items 9-14. Real ephemeral PostgreSQL/Gunicorn are used for the live-
ownership and workspace-cleanup tests (never a simulated/boolean stand-
in), mirroring scripts/tests/test_release_gate_corrective_v9_resource_
ownership.py's own house style.
"""

import shutil

import pytest

from scripts.release_gate import e2e_support, gate_runner, scenarios
from scripts.release_gate.cleanup import CleanupAggregateError, run_cleanup_actions
from scripts.release_gate.database import drop_database
from scripts.release_gate.e2e_support import author_and_package, dual_deployment

# --- RED test 25.9: live .llpkg ownership -----------------------------------


def test_llpkg_is_ledger_owned_while_physically_live(tmp_path):
    ledger = gate_runner.ResourceLedger()

    with dual_deployment(tmp_path, start_node=False, resource_ledger=ledger) as deployment:
        _authored, package_path = author_and_package(
            deployment,
            prompt="Which command lists files?",
            options=[{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
            correct_option_id="a",
        )
        assert package_path.exists() is True
        assert ledger.contains("temp_llpkg", str(package_path)) is True

    assert package_path.exists() is False
    assert ledger.contains("temp_llpkg", str(package_path)) is False


# --- RED test 25.10/25.11: live dump / live runtime log ownership ----------


def test_dump_and_runtime_log_files_are_ledger_owned_while_physically_live(tmp_path):
    ledger = gate_runner.ResourceLedger()

    with dual_deployment(tmp_path, resource_ledger=ledger) as deployment:
        central_log_path = deployment.tmp_dir / "central.log"
        node_log_path = deployment.tmp_dir / "node.log"
        assert central_log_path.exists() is True
        assert node_log_path.exists() is True
        assert ledger.contains("raw_runtime_log", str(central_log_path)) is True
        assert ledger.contains("raw_runtime_log", str(node_log_path)) is True

        try:
            detail = scenarios.run_backup_restore_smoke(deployment)

            central_dump_path = deployment.tmp_dir / "central.dump"
            node_dump_path = deployment.tmp_dir / "node.dump"
            assert central_dump_path.exists() is True
            assert node_dump_path.exists() is True
            assert ledger.contains("temp_dump", str(central_dump_path)) is True
            assert ledger.contains("temp_dump", str(node_dump_path)) is True
        finally:
            # Task 15 Task 11 corrective v8, Section 15/16: restore-
            # database disposal is normally gate_runner._run_one_
            # scenario()'s own CLEANUP-stage responsibility -- this test
            # calls the scenario function directly, so it owns that
            # lifecycle itself here (mirrors test_release_gate_backup_
            # restore.py's own baseline test).
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
    assert central_dump_path.exists() is False
    assert node_dump_path.exists() is False
    assert central_log_path.exists() is False
    assert ledger.contains("temp_dump", str(central_dump_path)) is False
    assert ledger.contains("temp_dump", str(node_dump_path)) is False
    assert ledger.contains("raw_runtime_log", str(central_log_path)) is False
    assert ledger.contains("raw_runtime_log", str(node_log_path)) is False


# --- RED test 25.12: workspace successful cleanup releases descendants ------


def test_workspace_successful_cleanup_releases_llpkg_and_log_entries(tmp_path):
    ledger = gate_runner.ResourceLedger()

    with dual_deployment(tmp_path, start_node=False, resource_ledger=ledger) as deployment:
        _authored, package_path = author_and_package(
            deployment,
            prompt="Which command lists files?",
            options=[{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
            correct_option_id="a",
        )
        log_path = deployment.tmp_dir / "central.log"
        workspace_path = deployment.tmp_dir
        assert ledger.contains("temp_llpkg", str(package_path)) is True
        assert ledger.contains("raw_runtime_log", str(log_path)) is True

    assert workspace_path.exists() is False
    assert ledger.contains("temp_llpkg", str(package_path)) is False
    assert ledger.contains("raw_runtime_log", str(log_path)) is False
    assert ledger.contains("workspace", str(workspace_path)) is False
    assert ledger.is_empty()


# --- RED test 25.13: workspace cleanup failure leaves descendants owned ----


def test_workspace_cleanup_failure_leaves_llpkg_and_log_entries_owned(tmp_path, monkeypatch):
    ledger = gate_runner.ResourceLedger()

    def _broken_remove_workspace(path):
        raise RuntimeError("simulated workspace removal failure -- not a name-lookup issue")

    monkeypatch.setattr(e2e_support, "remove_workspace", _broken_remove_workspace)

    with (
        pytest.raises(CleanupAggregateError),
        dual_deployment(tmp_path, start_node=False, resource_ledger=ledger) as deployment,
    ):
        _authored, package_path = author_and_package(
            deployment,
            prompt="Which command lists files?",
            options=[{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
            correct_option_id="a",
        )
        log_path = deployment.tmp_dir / "central.log"
        workspace_path = deployment.tmp_dir

    # Workspace disposal failed -- every descendant (and the workspace
    # itself) must remain ledger-owned, never released without verified
    # disposal.
    assert ledger.contains("temp_llpkg", str(package_path)) is True
    assert ledger.contains("raw_runtime_log", str(log_path)) is True
    assert ledger.contains("workspace", str(workspace_path)) is True

    # Real cleanup for the test's own deliberately-leaked workspace.
    shutil.rmtree(workspace_path, ignore_errors=True)


# --- RED test 25.14: individual child disposal ------------------------------


def test_individually_released_artifact_does_not_affect_sibling_entries():
    """No scenario code today deletes a .llpkg/dump/log file
    independently before workspace cleanup -- this proves the ledger
    mechanics themselves are per-resource independent, simulating what
    such independent disposal would look like directly against a
    ResourceLedger instance."""
    ledger = gate_runner.ResourceLedger()
    ledger.acquire("temp_llpkg", "/tmp/gate-workspace-x/package.llpkg")
    ledger.acquire("temp_dump", "/tmp/gate-workspace-x/central.dump")
    ledger.acquire("raw_runtime_log", "/tmp/gate-workspace-x/central.log")

    ledger.release("temp_llpkg", "/tmp/gate-workspace-x/package.llpkg")

    assert ledger.contains("temp_llpkg", "/tmp/gate-workspace-x/package.llpkg") is False
    assert ledger.contains("temp_dump", "/tmp/gate-workspace-x/central.dump") is True
    assert ledger.contains("raw_runtime_log", "/tmp/gate-workspace-x/central.log") is True
