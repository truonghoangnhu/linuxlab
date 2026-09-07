"""Task 15 Task 11 corrective v13 (design spec Sections 17-30, Part C
34-38): `parent_resource_id` becomes the SOLE authoritative structural
parent relation -- `children_of()` and `reconcile_verified_parent_
removal()` traverse by stable resource ID, never by the legacy
`(parent_kind, parent_safe_id)` locator tuple, so a stale legacy tuple
can never affect traversal/reconciliation, and a parent's own locator
relocation can never break an already-captured child relationship.

Before this corrective, `ResourceRef.parent_resource_id` already
existed (assigned automatically by `acquire()`/`relocate()`'s own
best-effort locator resolution -- corrective v12), but `children_of()`
and `reconcile_verified_parent_removal()` still matched structurally on
`(parent_kind, parent_safe_id)` -- a resource whose `parent_resource_id`
was correct but whose legacy tuple happened to be stale (or simply
never set) would not be found by either.

These are the mandatory RED tests from the v13 design spec, Section
34-38.
"""

import shutil

import pytest

from scripts.release_gate import gate_runner


def test_children_of_uses_parent_resource_id(tmp_path):
    ledger = gate_runner.ResourceLedger()
    workspace = tmp_path / "workspace"
    dump = workspace / "x.dump"

    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(workspace))
    child_id = ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        str(dump),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(workspace),
    )

    children = ledger.children_of(parent_id)
    assert {ref.resource_id for ref in children} == {child_id}


# --- Section 24/37: deliberate legacy/ID disagreement -----------------------


def test_children_of_finds_child_even_with_stale_legacy_tuple(tmp_path):
    workspace = tmp_path / "workspace"
    other_parent = tmp_path / "someone-elses-root"
    dump = workspace / "x.dump"

    ledger = gate_runner.ResourceLedger()
    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(workspace))
    child_id = ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        str(dump),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(workspace),
    )
    # Deliberately stale legacy tuple, disagreeing with the real
    # (correct) parent_resource_id captured above -- set directly via
    # relocate()'s own in-place record replacement, the lowest-level
    # supported mechanism for changing a record's stored fields,
    # without releasing/re-acquiring (which would mint a NEW
    # resource_id and defeat the point of this test).
    ledger.relocate(
        gate_runner.ResourceKind.TEMP_DUMP,
        str(dump),
        str(dump),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(other_parent),
    )
    # relocate() re-resolves parent_resource_id from the given legacy
    # tuple, which does not match any active resource here -- so the
    # record now carries a stale/absent legacy tuple. Restore the
    # correct parent_resource_id directly through set_parent(), exactly
    # as spec Section 24 describes ("parent_resource_id -> P1, legacy
    # tuple -> stale P0 locator").
    ledger.set_parent(child_id, parent_id)

    record = ledger.get(child_id)
    assert record.parent_resource_id == parent_id
    assert record.parent_safe_id == str(other_parent)  # legacy tuple stays stale

    children = ledger.children_of(parent_id)
    assert {ref.resource_id for ref in children} == {child_id}


# --- Section 19/35: parent relocation preserves child traversal -------------


def test_parent_relocation_preserves_child_traversal(tmp_path):
    ledger = gate_runner.ResourceLedger()
    old_workspace = tmp_path / "workspace"
    new_workspace = tmp_path / "workspace-renamed"
    dump = old_workspace / "x.dump"

    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(old_workspace))
    child_id = ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        str(dump),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(old_workspace),
    )

    ledger.relocate(gate_runner.ResourceKind.WORKSPACE, str(old_workspace), str(new_workspace))

    children = ledger.children_of(parent_id)
    assert {ref.resource_id for ref in children} == {child_id}


# --- Section 20/41: real parent-relocation reconciliation -------------------


def test_reconcile_by_parent_id_after_verified_removal_following_relocation(tmp_path):
    """Mirrors spec Section 20's mandatory lifecycle: acquire parent,
    acquire child under it, relocate the parent, physically remove the
    relocated parent, reconcile by the parent's stable ID -- the child
    (whose own locator is now verifiably absent, nested under the
    relocated parent) is released, and reconciliation still works
    despite the parent's ORIGINAL locator no longer existing anywhere."""
    ledger = gate_runner.ResourceLedger()
    old_workspace = tmp_path / "workspace"
    new_workspace = tmp_path / "workspace-moved"
    old_workspace.mkdir()
    dump = old_workspace / "x.dump"
    dump.write_bytes(b"data")

    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(old_workspace))
    child_id = ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        str(dump),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(old_workspace),
    )

    old_workspace.rename(new_workspace)
    ledger.relocate(gate_runner.ResourceKind.WORKSPACE, str(old_workspace), str(new_workspace))
    # The child's own ledger locator is deliberately left pointing at
    # the OLD (now doubly-gone, since the directory was both renamed
    # away AND the new location is about to be removed) path -- proving
    # reconciliation finds it via parent_resource_id (unaffected by the
    # parent's locator change) and independently re-verifies ITS OWN
    # locator absent, exactly as spec Section 20 requires.
    shutil.rmtree(new_workspace)
    ledger.reconcile_verified_parent_removal(parent_id, str(new_workspace))
    ledger.release(gate_runner.ResourceKind.WORKSPACE, str(new_workspace))

    assert ledger.get(child_id) is None
    assert ledger.is_empty()


# --- Section 21/36: set_parent() ID-native semantics -------------------------


def test_set_parent_updates_the_id_native_graph_immediately(tmp_path):
    ledger = gate_runner.ResourceLedger()
    workspace = tmp_path / "workspace"
    other_root = tmp_path / "other-root"
    dump = tmp_path / "orphan.dump"

    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(workspace))
    ledger.acquire(gate_runner.ResourceKind.OUTER_ROOT, str(other_root))
    child_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, str(dump))

    assert ledger.children_of(parent_id) == ()

    ledger.set_parent(child_id, parent_id)

    assert ledger.get(child_id).parent_resource_id == parent_id
    assert {ref.resource_id for ref in ledger.children_of(parent_id)} == {child_id}


def test_set_parent_to_none_clears_the_relationship(tmp_path):
    ledger = gate_runner.ResourceLedger()
    workspace = tmp_path / "workspace"
    dump = workspace / "x.dump"

    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(workspace))
    child_id = ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        str(dump),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(workspace),
    )

    ledger.set_parent(child_id, None)

    assert ledger.get(child_id).parent_resource_id is None
    assert ledger.children_of(parent_id) == ()


def test_set_parent_rejects_unknown_child(tmp_path):
    ledger = gate_runner.ResourceLedger()
    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(tmp_path / "workspace"))
    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.set_parent("not-a-real-resource-id", parent_id)


def test_set_parent_rejects_unknown_parent(tmp_path):
    ledger = gate_runner.ResourceLedger()
    child_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, str(tmp_path / "x.dump"))
    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.set_parent(child_id, "not-a-real-resource-id")


def test_set_parent_rejects_self_parenting(tmp_path):
    ledger = gate_runner.ResourceLedger()
    resource_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(tmp_path / "workspace"))
    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.set_parent(resource_id, resource_id)


def test_set_parent_rejects_a_two_node_cycle(tmp_path):
    ledger = gate_runner.ResourceLedger()
    a_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(tmp_path / "a"))
    b_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(tmp_path / "b"))
    ledger.set_parent(b_id, a_id)

    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.set_parent(a_id, b_id)

    # The rejected attempt must not have mutated the existing, valid
    # relationship.
    assert ledger.get(a_id).parent_resource_id is None
    assert ledger.get(b_id).parent_resource_id == a_id


# --- Section 29/38: child relocated outside its parent stays owned ----------


def test_child_relocated_outside_parent_survives_reconciliation(tmp_path):
    ledger = gate_runner.ResourceLedger()
    parent_dir = tmp_path / "workspace"
    parent_dir.mkdir()
    child_file = parent_dir / "central.dump"
    child_file.write_bytes(b"data")
    external_file = tmp_path / "external.dump"

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
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, str(external_file)) is True
    assert ledger.retained_execution_resources() != ()
