"""Task 15 Task 11 corrective v11 (design spec Sections 13-25): ledger
mechanics for structural (never path-string-prefix) resource ownership.

`ResourceRef` previously carried no parent/child relationship at all --
reconciliation after a verified parent removal (gate_runner's own outer-
root sweep, and e2e_support.dual_deployment()'s own workspace sweep) was
done by matching `ref.safe_id.startswith(parent_path)`, a STRING prefix
match. `/tmp/run` and `/tmp/run-outside/file.dump` share a string prefix
but are not parent/child -- removing `/tmp/run` must never release that
sibling. These are the mandatory RED tests from the v11 design spec,
Section 32, items 6-10, exercised directly against `ResourceLedger` (the
same house style as
scripts/tests/test_release_gate_corrective_v10_artifact_ownership.py's
own `test_individually_released_artifact_does_not_affect_sibling_
entries` -- real filesystem paths/removal, never a simulated boolean
stand-in for "exists").
"""

import shutil

from scripts.release_gate import gate_runner

# --- RED test 32.6: same-prefix sibling --------------------------------------


def test_same_prefix_sibling_is_not_released_by_parent_removal(tmp_path):
    parent_dir = tmp_path / "run"
    parent_dir.mkdir()
    sibling_file = tmp_path / "run-outside" / "file.dump"
    sibling_file.parent.mkdir()
    sibling_file.write_bytes(b"data")
    assert str(sibling_file).startswith(str(parent_dir))  # genuine string-prefix collision

    ledger = gate_runner.ResourceLedger()
    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))
    # Registered WITHOUT parent=parent_dir -- it is not this parent's child.
    ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, str(sibling_file))

    shutil.rmtree(parent_dir)
    ledger.reconcile_verified_parent_removal(parent_id, str(parent_dir))
    ledger.release(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))

    assert sibling_file.exists() is True
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, str(sibling_file)) is True
    assert ledger.retained_execution_resources() != ()


# --- RED test 32.7: explicit child, successful parent removal ---------------


def test_explicit_child_released_after_verified_parent_removal(tmp_path):
    parent_dir = tmp_path / "workspace"
    parent_dir.mkdir()
    child_file = parent_dir / "package.llpkg"
    child_file.write_bytes(b"data")

    ledger = gate_runner.ResourceLedger()
    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))
    ledger.acquire(
        gate_runner.ResourceKind.TEMP_LLPKG,
        str(child_file),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(parent_dir),
    )

    shutil.rmtree(parent_dir)
    ledger.reconcile_verified_parent_removal(parent_id, str(parent_dir))
    ledger.release(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))

    assert ledger.is_empty()


def test_reconcile_cascades_through_grandchildren(tmp_path):
    """Mirrors the real two-level hierarchy (OUTER_ROOT -> WORKSPACE ->
    TEMP_LLPKG/TEMP_DUMP/RAW_RUNTIME_LOG): a single verified-successful
    removal at the OUTER_ROOT level must cascade release through every
    still-registered descendant, not just its direct children."""
    outer_root = tmp_path / "outer"
    outer_root.mkdir()
    workspace = outer_root / "gate-workspace-x"
    workspace.mkdir()
    llpkg = workspace / "package.llpkg"
    llpkg.write_bytes(b"data")

    ledger = gate_runner.ResourceLedger()
    outer_root_id = ledger.acquire(gate_runner.ResourceKind.OUTER_ROOT, str(outer_root))
    ledger.acquire(
        gate_runner.ResourceKind.WORKSPACE,
        str(workspace),
        parent_kind=gate_runner.ResourceKind.OUTER_ROOT,
        parent_safe_id=str(outer_root),
    )
    ledger.acquire(
        gate_runner.ResourceKind.TEMP_LLPKG,
        str(llpkg),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(workspace),
    )

    shutil.rmtree(outer_root)
    ledger.reconcile_verified_parent_removal(outer_root_id, str(outer_root))
    ledger.release(gate_runner.ResourceKind.OUTER_ROOT, str(outer_root))

    assert ledger.is_empty()


# --- RED test 32.8: moved external child -------------------------------------


def test_moved_external_child_survives_parent_removal_via_relocate(tmp_path):
    parent_dir = tmp_path / "workspace"
    parent_dir.mkdir()
    child_file = parent_dir / "central.dump"
    child_file.write_bytes(b"data")
    external_file = tmp_path / "external.dump"

    ledger = gate_runner.ResourceLedger()
    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))
    ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        str(child_file),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(parent_dir),
    )

    child_file.rename(external_file)
    ledger.relocate(gate_runner.ResourceKind.TEMP_DUMP, str(child_file), str(external_file))

    shutil.rmtree(parent_dir)
    ledger.reconcile_verified_parent_removal(parent_id, str(parent_dir))
    ledger.release(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))

    assert external_file.exists() is True
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, str(child_file)) is False
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, str(external_file)) is True
    assert ledger.retained_execution_resources() != ()


# --- RED test 32.9: direct child delete --------------------------------------


def test_direct_child_delete_releases_only_that_child():
    ledger = gate_runner.ResourceLedger()
    ledger.acquire(
        gate_runner.ResourceKind.TEMP_LLPKG,
        "/tmp/gate-workspace-x/package.llpkg",
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id="/tmp/gate-workspace-x",
    )
    ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        "/tmp/gate-workspace-x/central.dump",
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id="/tmp/gate-workspace-x",
    )

    ledger.release(gate_runner.ResourceKind.TEMP_LLPKG, "/tmp/gate-workspace-x/package.llpkg")

    assert ledger.contains(gate_runner.ResourceKind.TEMP_LLPKG, "/tmp/gate-workspace-x/package.llpkg") is False
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, "/tmp/gate-workspace-x/central.dump") is True


# --- RED test 32.10: parent cleanup failure ----------------------------------


def test_reconcile_does_not_release_children_whose_locator_still_exists(tmp_path):
    """A defensive property of reconcile_verified_parent_removal()
    itself: it must never release a child whose own locator is still
    verifiably present, regardless of what the caller believes about
    the parent -- callers only ever invoke it AFTER a parent's own
    removal has already been verified successful, but the function
    itself does not blindly trust that; every child is independently
    re-verified."""
    parent_dir = tmp_path / "workspace"
    parent_dir.mkdir()
    child_file = parent_dir / "central.log"
    child_file.write_bytes(b"data")

    ledger = gate_runner.ResourceLedger()
    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))
    ledger.acquire(
        gate_runner.ResourceKind.RAW_RUNTIME_LOG,
        str(child_file),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(parent_dir),
    )

    ledger.reconcile_verified_parent_removal(parent_id, str(parent_dir))

    assert child_file.exists() is True
    assert ledger.contains(gate_runner.ResourceKind.RAW_RUNTIME_LOG, str(child_file)) is True
    assert ledger.contains(gate_runner.ResourceKind.WORKSPACE, str(parent_dir)) is True
