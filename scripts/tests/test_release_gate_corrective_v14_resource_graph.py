"""Task 15 Task 11 corrective v14 (design spec Sections 6, 9): closes
the independent v13 review's structural-ownership findings.

1. `reconcile_verified_parent_removal()` used to fall back to genuine
   path-CONTAINMENT when a resource carried no matching explicit
   `parent_resource_id` -- an explicit parent Q could be silently
   overridden by locator containment under a DIFFERENT parent P being
   reconciled, and an unparented resource whose locator merely
   happened to sit under P could be auto-released as if P owned it.
   Both are now fail-closed: only `parent_resource_id` decides.
2. `relocate()` used to accept `parent_kind`/`parent_safe_id` keyword
   arguments and silently RE-RESOLVE `parent_resource_id` as a side
   effect of a pure locator move -- reparenting and relocation are now
   strictly separate operations (only `set_parent()` may reparent), and
   `relocate()` itself is fully atomic (every precondition validated
   before any mutation).
3. `acquire()` used to silently create a "half-parented" record (a
   legacy tuple pointing at nothing, `parent_resource_id=None`) when a
   supplied `parent_kind`/`parent_safe_id` pair didn't resolve to an
   active resource -- it now fails closed instead.
"""

import shutil

import pytest

from scripts.release_gate import gate_runner

# --- Section 6.1/6.2: explicit parent_resource_id beats containment --------


def test_reconcile_does_not_override_explicit_parent_id_with_locator_containment(tmp_path):
    p_dir = tmp_path / "p"
    q_dir = tmp_path / "q"
    p_dir.mkdir()
    q_dir.mkdir()
    child = p_dir / "child.dump"
    child.write_bytes(b"data")

    ledger = gate_runner.ResourceLedger()
    p_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(p_dir))
    q_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(q_dir))
    child_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, str(child))
    ledger.set_parent(child_id, q_id)

    shutil.rmtree(p_dir)
    ledger.reconcile_verified_parent_removal(p_id, str(p_dir))

    # The child's locator was physically UNDER P, but it is structurally
    # parented to Q -- reconciling P must never touch it.
    assert ledger.get(child_id) is not None
    assert ledger.get(child_id).parent_resource_id == q_id
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, str(child)) is True


def test_reconcile_does_not_infer_parent_for_unparented_resource(tmp_path):
    parent = tmp_path / "workspace"
    parent.mkdir()
    child = parent / "orphan.dump"
    child.write_bytes(b"data")

    ledger = gate_runner.ResourceLedger()
    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(parent))
    child_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, str(child))

    shutil.rmtree(parent)
    ledger.reconcile_verified_parent_removal(parent_id, str(parent))

    # Ownership is uncertain (no explicit parent link at all) -- this
    # must fail CLOSED: the record stays retained, never inferred as
    # P's child merely because its locator sat underneath P.
    assert ledger.get(child_id) is not None
    assert ledger.get(child_id).parent_resource_id is None
    assert child_id in {ref.resource_id for ref in ledger.retained_execution_resources()}


def test_reconcile_with_none_parent_id_is_a_pure_no_op(tmp_path):
    """Section 6.2: `parent_resource_id=None` means exactly "no explicit
    children" now -- no further fallback behavior of any kind."""
    orphan = tmp_path / "orphan.dump"
    orphan.write_bytes(b"data")
    ledger = gate_runner.ResourceLedger()
    orphan_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, str(orphan))

    ledger.reconcile_verified_parent_removal(None, str(tmp_path))

    assert ledger.get(orphan_id) is not None


# --- Section 6.3: acquire() fails closed on a non-resolving legacy pair ----


def test_acquire_rejects_non_resolving_legacy_parent_pair(tmp_path):
    ledger = gate_runner.ResourceLedger()
    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.acquire(
            gate_runner.ResourceKind.TEMP_DUMP,
            str(tmp_path / "x.dump"),
            parent_kind=gate_runner.ResourceKind.WORKSPACE,
            parent_safe_id=str(tmp_path / "never-acquired-workspace"),
        )


def test_acquire_rejects_non_resolving_legacy_parent_pair_leaves_ledger_unchanged(tmp_path):
    ledger = gate_runner.ResourceLedger()
    locator = str(tmp_path / "x.dump")
    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.acquire(
            gate_runner.ResourceKind.TEMP_DUMP,
            locator,
            parent_kind=gate_runner.ResourceKind.WORKSPACE,
            parent_safe_id=str(tmp_path / "never-acquired-workspace"),
        )
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, locator) is False
    assert ledger.is_empty()


def test_acquire_without_any_parent_arguments_remains_the_legitimate_no_parent_case(tmp_path):
    ledger = gate_runner.ResourceLedger()
    resource_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, str(tmp_path / "x.dump"))
    assert ledger.get(resource_id).parent_resource_id is None


# --- Section 6.4/6.5: relocate() is locator-only and fully atomic ----------


def test_relocate_no_longer_accepts_parent_arguments():
    ledger = gate_runner.ResourceLedger()
    ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, "/tmp/a.dump")
    with pytest.raises(TypeError):
        ledger.relocate(
            gate_runner.ResourceKind.TEMP_DUMP,
            "/tmp/a.dump",
            "/tmp/b.dump",
            parent_kind=gate_runner.ResourceKind.WORKSPACE,
            parent_safe_id="/tmp/workspace",
        )


def test_relocate_never_changes_parent_resource_id(tmp_path):
    ledger = gate_runner.ResourceLedger()
    workspace = tmp_path / "workspace"
    dump = workspace / "x.dump"
    moved_dump = workspace / "x-moved.dump"

    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(workspace))
    child_id = ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        str(dump),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(workspace),
    )

    ledger.relocate(gate_runner.ResourceKind.TEMP_DUMP, str(dump), str(moved_dump))

    assert ledger.get(child_id).parent_resource_id == parent_id
    assert ledger.get(child_id).safe_id == str(moved_dump)


def test_relocate_rejects_active_locator_collision_without_mutation(tmp_path):
    ledger = gate_runner.ResourceLedger()
    a = str(tmp_path / "a.dump")
    b = str(tmp_path / "b.dump")
    a_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, a)
    b_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, b)

    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.relocate(gate_runner.ResourceKind.TEMP_DUMP, a, b)

    assert ledger.get(a_id).safe_id == a
    assert ledger.get(b_id).safe_id == b
    assert ledger.get_by_locator(gate_runner.ResourceKind.TEMP_DUMP, a).resource_id == a_id
    assert ledger.get_by_locator(gate_runner.ResourceKind.TEMP_DUMP, b).resource_id == b_id


def test_relocate_rejects_an_unowned_source_and_leaves_the_ledger_unchanged(tmp_path):
    ledger = gate_runner.ResourceLedger()
    existing = str(tmp_path / "existing.dump")
    existing_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, existing)

    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.relocate(gate_runner.ResourceKind.TEMP_DUMP, str(tmp_path / "never-acquired.dump"), str(tmp_path / "new.dump"))

    assert ledger.get(existing_id).safe_id == existing
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, str(tmp_path / "new.dump")) is False


def test_relocate_to_the_same_locator_is_a_safe_no_op(tmp_path):
    ledger = gate_runner.ResourceLedger()
    locator = str(tmp_path / "a.dump")
    resource_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, locator)

    returned_id = ledger.relocate(gate_runner.ResourceKind.TEMP_DUMP, locator, locator)

    assert returned_id == resource_id
    assert ledger.get(resource_id).safe_id == locator


def test_relocate_returns_the_stable_resource_id(tmp_path):
    ledger = gate_runner.ResourceLedger()
    old_locator = str(tmp_path / "a.dump")
    new_locator = str(tmp_path / "b.dump")
    resource_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, old_locator)

    returned_id = ledger.relocate(gate_runner.ResourceKind.TEMP_DUMP, old_locator, new_locator)

    assert returned_id == resource_id


# --- Section 6.6: set_parent() rejection is atomic --------------------------


def test_set_parent_rejected_cycle_mutation_is_atomic(tmp_path):
    ledger = gate_runner.ResourceLedger()
    a_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(tmp_path / "a"))
    b_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(tmp_path / "b"))
    c_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(tmp_path / "c"))
    ledger.set_parent(b_id, a_id)
    ledger.set_parent(c_id, b_id)

    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.set_parent(a_id, c_id)

    assert ledger.get(a_id).parent_resource_id is None
    assert ledger.get(b_id).parent_resource_id == a_id
    assert ledger.get(c_id).parent_resource_id == b_id


# --- Section 6.8/6.9: same-prefix and partial-dump identity remain closed --


def test_same_prefix_sibling_remains_owned_after_reconciliation_without_fallback(tmp_path):
    parent_dir = tmp_path / "run"
    parent_dir.mkdir()
    sibling_file = tmp_path / "run-outside" / "file.dump"
    sibling_file.parent.mkdir()
    sibling_file.write_bytes(b"data")

    ledger = gate_runner.ResourceLedger()
    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))
    sibling_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, str(sibling_file))

    shutil.rmtree(parent_dir)
    ledger.reconcile_verified_parent_removal(parent_id, str(parent_dir))
    ledger.release(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))

    assert sibling_file.exists() is True
    assert ledger.get(sibling_id) is not None
    assert ledger.retained_execution_resources() != ()


def test_partial_dump_output_keeps_the_same_resource_id_through_producer_failure(tmp_path):
    ledger = gate_runner.ResourceLedger()
    workspace = tmp_path / "workspace"
    dump_path = workspace / "node.dump"
    ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(workspace))

    reserved_id = ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        str(dump_path),
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=str(workspace),
    )

    class _PartialWriteFailure(Exception):
        pass

    workspace.mkdir()
    try:
        with open(dump_path, "wb") as handle:
            handle.write(b"partial-bytes")
        raise _PartialWriteFailure("producer failed partway through")
    except _PartialWriteFailure:
        pass

    record = ledger.get(reserved_id)
    assert record is not None
    assert record.resource_id == reserved_id
    assert record.safe_id == str(dump_path)
    assert reserved_id in {ref.resource_id for ref in ledger.retained_execution_resources()}
