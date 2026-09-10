"""Task 15 Task 11 corrective v14 (design spec Section 6.7, Section 9):
a durable, checked-in real `run_release_gate()` + real `dual_deployment()`
parent-relocation lifecycle test -- proving, end to end through the
ACTUAL production runner (not isolated `ResourceLedger` calls, and not
fakes), that:

1. acquiring a parent P, then a child C under P;
2. relocating P to a new locator (P's `resource_id` unchanged, C's
   `parent_resource_id` unchanged, `children_of(P.resource_id)` still
   returns C);
3. physically removing the relocated parent;
4. reconciling by `P.resource_id`;

releases C only once its OWN locator is independently verified absent,
and never depends on -- or is defeated by -- locator containment
(corrective v14 removed that fallback entirely from `reconcile_
verified_parent_removal()`).

Mirrors the house style of
scripts/tests/test_release_gate_corrective_v13_real_lifecycle.py.
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
def test_real_run_parent_relocation_lifecycle_reconciles_by_stable_id(tmp_path):
    """Section 6.7's full ten-step lifecycle, proven inside a REAL
    scenario running through the real production runner/deployment."""
    lifecycle_state: dict = {}

    def _relocate_parent_inside_real_deployment(deployment):
        ledger = deployment.resource_ledger
        workspace = deployment.tmp_dir
        parent_old = workspace / "relocatable-parent"
        parent_new = workspace / "relocated-parent"
        child_file = parent_old / "child.dump"

        parent_old.mkdir()
        child_file.write_bytes(b"real-parent-relocation-lifecycle")

        workspace_ref = ledger.get_by_locator("workspace", str(workspace))
        assert workspace_ref is not None

        # 1. acquire parent P (registered as the real workspace's own
        #    explicit structural child, matching real production nesting).
        parent_id = ledger.acquire(
            "workspace", str(parent_old), parent_kind="workspace", parent_safe_id=str(workspace)
        )
        # 2. acquire child C under P.
        child_id = ledger.acquire(
            "temp_dump", str(child_file), parent_kind="workspace", parent_safe_id=str(parent_old)
        )

        # 3. relocate P to a new locator.
        parent_old.rename(parent_new)
        returned_id = ledger.relocate("workspace", str(parent_old), str(parent_new))

        # 4/5/6: P retains its resource_id; C retains parent_resource_id
        # == P.resource_id; children_of(P.resource_id) still returns C.
        assert returned_id == parent_id
        assert ledger.get(parent_id).resource_id == parent_id
        assert ledger.get(child_id).parent_resource_id == parent_id
        assert {ref.resource_id for ref in ledger.children_of(parent_id)} == {child_id}

        # 7. physically remove the relocated parent.
        shutil.rmtree(parent_new)
        # 8. reconcile using P.resource_id.
        ledger.reconcile_verified_parent_removal(parent_id, str(parent_new))
        ledger.release("workspace", str(parent_new))

        # 9. the descendant (its own locator now independently verified
        #    absent) is released.
        lifecycle_state["parent_released"] = ledger.get(parent_id) is None
        lifecycle_state["child_released"] = ledger.get(child_id) is None

    result = gate_runner.run_release_gate(
        repo_root=gate_runner.REPO_ROOT,
        check_platform=False,
        keep_on_failure=False,
        scenario_specs=(
            ("integration_real_lifecycle_parent_relocation", _relocate_parent_inside_real_deployment),
        ),
        run_validation=lambda env: dict(_OK_VALIDATION),
        tmp_root=tmp_path,
    )

    assert lifecycle_state["parent_released"] is True
    assert lifecycle_state["child_released"] is True

    # A custom single-scenario `scenario_specs` never matches the fixed
    # real seven required scenarios, so this run can never reach real
    # PASS -- what this test proves is narrower: truthful, non-retained
    # execution-resource state after a REAL parent-relocation lifecycle
    # through the actual production wiring.
    assert result.overall == "CLEANUP_FAILURE"
    assert result.evidence is not None
    assert result.evidence.retention["execution_resources_retained"] is False
    run_id = result.evidence.run["run_id"]
    assert not any(tmp_path.glob(f"phase1-release-{run_id}-*"))


@pytest.mark.skipif(not _working_tree_is_clean(), reason="requires a clean working tree (real preflight)")
def test_real_run_surviving_relocated_child_blocks_pass_truthfully(tmp_path):
    """Converse of the lifecycle above: a child explicitly parented to P,
    then itself relocated OUTSIDE P before P is removed, must survive
    real reconciliation and be truthfully reported as retained --
    corrective v14's removal of the containment fallback must never
    cause a genuinely-owned surviving resource to go unreported either."""
    survivor_holder: dict = {}

    def _relocate_child_outside_its_real_parent(deployment):
        ledger = deployment.resource_ledger
        workspace = deployment.tmp_dir
        parent_dir = workspace / "doomed-parent"
        parent_dir.mkdir()
        child_file = parent_dir / "survivor.dump"
        child_file.write_bytes(b"real-survivor-bytes")
        # Deliberately OUTSIDE the run's own outer temp root (tmp_root_path
        # is nested directly under the test's own tmp_path, never under
        # the workspace) -- otherwise the run's own outer-root cleanup
        # would physically sweep this "external" location away too,
        # defeating the point of proving it independently SURVIVES.
        external_dir = tmp_path / f"external-survivor-{workspace.name}"
        external_dir.mkdir()
        external_file = external_dir / "survivor.dump"

        parent_id = ledger.acquire(
            "workspace", str(parent_dir), parent_kind="workspace", parent_safe_id=str(workspace)
        )
        child_id = ledger.acquire(
            "temp_dump", str(child_file), parent_kind="workspace", parent_safe_id=str(parent_dir)
        )

        child_file.rename(external_file)
        ledger.relocate("temp_dump", str(child_file), str(external_file))

        shutil.rmtree(parent_dir)
        ledger.reconcile_verified_parent_removal(parent_id, str(parent_dir))
        ledger.release("workspace", str(parent_dir))

        survivor_holder["external_dir"] = external_dir
        survivor_holder["external_file"] = external_file
        survivor_holder["child_still_owned"] = ledger.get(child_id) is not None

    result = gate_runner.run_release_gate(
        repo_root=gate_runner.REPO_ROOT,
        check_platform=False,
        keep_on_failure=False,
        scenario_specs=(
            ("integration_real_lifecycle_surviving_relocated_child", _relocate_child_outside_its_real_parent),
        ),
        run_validation=lambda env: dict(_OK_VALIDATION),
        tmp_root=tmp_path,
    )

    try:
        assert survivor_holder["child_still_owned"] is True
        assert survivor_holder["external_file"].exists() is True
        assert result.overall != "PASS"
        assert result.evidence is not None
        assert result.evidence.retention["execution_resources_retained"] is True
    finally:
        shutil.rmtree(survivor_holder["external_dir"], ignore_errors=True)
