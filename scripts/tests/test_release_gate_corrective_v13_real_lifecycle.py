"""Task 15 Task 11 corrective v13 (design spec Sections 25/41, Part C
41): a runner-level test combining the REAL gate_runner.run_release_gate()
orchestration with the REAL dual_deployment() (not fakes, not isolated
ResourceLedger calls) -- proving the ACTUAL production call-site wiring
(gate_runner._run_release_gate()'s own `outer_root_id` capture, and
e2e_support.dual_deployment()'s own `workspace_id` capture, both
threaded into `reconcile_verified_parent_removal()`) is correct end to
end, not merely that `ResourceLedger` itself behaves correctly in
isolation (already covered by
test_release_gate_corrective_v13_parent_id_ownership.py).

Mirrors the house style of
scripts/tests/test_release_gate_keep_on_failure_integration.py -- a
real `run_release_gate()` call with a lightweight custom scenario,
`run_validation` faked to skip the real (slow, unrelated)
`manage.py check` subprocess calls.
"""

import shutil
import subprocess

import pytest

from scripts.release_gate import gate_runner

_OK_VALIDATION = {
    "clean_tree": True,
    "central_check": "ok",
    "node_check": "ok",
    "central_migration_drift": "none",
    "node_migration_drift": "none",
}


def _working_tree_is_clean() -> bool:
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True)
    return result.stdout.strip() == ""


@pytest.mark.skipif(not _working_tree_is_clean(), reason="requires a clean working tree (real preflight)")
def test_real_run_reconciles_workspace_children_by_parent_id_after_verified_removal(tmp_path):
    """The ordinary, successful case: a real scenario runs inside a
    real dual_deployment() workspace, and normal teardown removes it.
    Reconciliation for BOTH the workspace (e2e_support.py's own
    `workspace_id` wiring) and the outer root (gate_runner.py's own
    `outer_root_id` wiring) must complete with no false retention,
    proving the resource_id captured before each `release()` call
    really does reach `reconcile_verified_parent_removal()`."""

    def _real_happy_scenario(deployment):
        return None

    result = gate_runner.run_release_gate(
        repo_root=gate_runner.REPO_ROOT,
        check_platform=False,
        keep_on_failure=False,
        scenario_specs=(("integration_real_lifecycle_happy", _real_happy_scenario),),
        run_validation=lambda env: dict(_OK_VALIDATION),
        tmp_root=tmp_path,
    )

    # A custom single-scenario `scenario_specs` never matches
    # evidence.REQUIRED_SCENARIOS (the fixed real seven), so
    # validate_for_pass() always rejects the provisional candidate on
    # "required_scenario_set_mismatch" -- this run can therefore never
    # reach PASS regardless of resource cleanup. What this test proves
    # is narrower and specific to this corrective: truthful, non-
    # retained execution-resource state after a REAL run through the
    # actual production wiring, not PASS eligibility itself (already
    # covered by the real seven-scenario gate elsewhere).
    assert result.overall == "CLEANUP_FAILURE"
    assert result.evidence is not None
    assert result.evidence.retention["execution_resources_retained"] is False
    run_id = result.evidence.run["run_id"]
    assert not any(tmp_path.glob(f"phase1-release-{run_id}-*"))


@pytest.mark.skipif(not _working_tree_is_clean(), reason="requires a clean working tree (real preflight)")
def test_real_run_child_relocated_outside_workspace_survives_reconciliation(tmp_path):
    """Section 29/41's adversarial case, proven through the REAL
    runner: a real child resource physically relocated OUTSIDE its
    workspace mid-scenario must survive that workspace's own real,
    verified removal -- reconciliation must follow `parent_resource_id`
    (never a path-string prefix), so the relocated child is correctly
    NOT released, execution retention is truthfully reported, and PASS
    is impossible."""
    external_dir = tmp_path / "external-survivor"
    external_dir.mkdir()
    relocated_holder: dict = {}

    def _relocate_a_real_child_outside_its_workspace(deployment):
        ledger = deployment.resource_ledger
        workspace = deployment.tmp_dir
        child_file = workspace / "artifact.dump"
        child_file.write_bytes(b"real-artifact-bytes")
        ledger.acquire(
            "temp_dump",
            str(child_file),
            parent_kind="workspace",
            parent_safe_id=str(workspace),
        )

        external_file = external_dir / "artifact.dump"
        child_file.rename(external_file)
        ledger.relocate("temp_dump", str(child_file), str(external_file))
        relocated_holder["external_file"] = external_file

    result = gate_runner.run_release_gate(
        repo_root=gate_runner.REPO_ROOT,
        check_platform=False,
        keep_on_failure=False,
        scenario_specs=(
            ("integration_real_lifecycle_relocated_child", _relocate_a_real_child_outside_its_workspace),
        ),
        run_validation=lambda env: dict(_OK_VALIDATION),
        tmp_root=tmp_path,
    )

    try:
        external_file = relocated_holder["external_file"]
        assert external_file.exists() is True

        assert result.overall != "PASS"
        assert result.evidence is not None
        assert result.evidence.retention["execution_resources_retained"] is True
    finally:
        shutil.rmtree(external_dir, ignore_errors=True)


@pytest.mark.skipif(not _working_tree_is_clean(), reason="requires a clean working tree (real preflight)")
def test_real_run_stale_legacy_tuple_does_not_affect_real_reconciliation(tmp_path):
    """Section 24/37's deliberate legacy/ID disagreement, proven inside
    a REAL scenario: a real child resource whose `parent_resource_id`
    is correct but whose legacy `(parent_kind, parent_safe_id)` tuple
    is deliberately stale must still be found and released by the real
    production reconciliation call path, which is ID-native only."""

    def _register_child_with_stale_legacy_tuple(deployment):
        ledger = deployment.resource_ledger
        workspace = deployment.tmp_dir
        workspace_id = ledger.get_by_locator("workspace", str(workspace)).resource_id

        child_file = workspace / "artifact.dump"
        child_file.write_bytes(b"real-artifact-bytes")
        child_id = ledger.acquire(
            "temp_dump",
            str(child_file),
            parent_kind="workspace",
            parent_safe_id=str(workspace),
        )
        # Deliberately stale legacy tuple (never touching the real,
        # correct parent_resource_id already captured above by
        # acquire()'s own locator resolution).
        stale_parent_safe_id = str(workspace) + "-stale-legacy-locator"
        ledger.relocate(
            "temp_dump",
            str(child_file),
            str(child_file),
            parent_kind="workspace",
            parent_safe_id=stale_parent_safe_id,
        )
        ledger.set_parent(child_id, workspace_id)
        assert ledger.get(child_id).parent_safe_id == stale_parent_safe_id
        assert ledger.get(child_id).parent_resource_id == workspace_id

    result = gate_runner.run_release_gate(
        repo_root=gate_runner.REPO_ROOT,
        check_platform=False,
        keep_on_failure=False,
        scenario_specs=(
            ("integration_real_lifecycle_stale_legacy_tuple", _register_child_with_stale_legacy_tuple),
        ),
        run_validation=lambda env: dict(_OK_VALIDATION),
        tmp_root=tmp_path,
    )

    # Same rationale as the happy-path test above: a custom scenario
    # name can never reach real PASS. What matters here is that the
    # stale legacy tuple never blocked correct, truthful cleanup.
    assert result.overall == "CLEANUP_FAILURE"
    assert result.evidence is not None
    assert result.evidence.retention["execution_resources_retained"] is False
    run_id = result.evidence.run["run_id"]
    assert not any(tmp_path.glob(f"phase1-release-{run_id}-*"))
