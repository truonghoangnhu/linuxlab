"""Task 15 Task 11 corrective v12 (design spec Sections 14-23): stable
opaque `resource_id` identity for `ResourceLedger`.

Before this corrective, a resource's IDENTITY was `(kind, safe_id)`
itself -- `relocate()` was literally implemented as `release(old) +
acquire(new)` (spec Section 20 explicitly forbids this pattern), so a
resource that physically moved became, from the ledger's own point of
view, an entirely unrelated new resource with no traceable connection to
the one that used to occupy the old locator. Repeated acquisition of the
same active `(kind, safe_id)` was also silently accepted (Python
`set.add()` on an already-equal element is a no-op that keeps the
ORIGINAL entry), which could let a caller believe stale parent metadata
had been updated when it had not.

These are the mandatory RED tests from the v12 design spec, Section
31-34.
"""

import pytest

from scripts.release_gate import gate_runner

# --- Section 31: stable resource identity ------------------------------------


def test_acquire_returns_a_stable_resource_id():
    ledger = gate_runner.ResourceLedger()
    resource_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, "/tmp/workspace-a")
    assert isinstance(resource_id, str)
    assert resource_id != ""


def test_relocate_preserves_resource_id_independent_of_locator(tmp_path):
    ledger = gate_runner.ResourceLedger()
    old_locator = str(tmp_path / "a.dump")
    new_locator = str(tmp_path / "moved.dump")

    resource_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, old_locator)
    ledger.relocate(gate_runner.ResourceKind.TEMP_DUMP, old_locator, new_locator)

    record = ledger.get(resource_id)
    assert record is not None
    assert record.resource_id == resource_id
    assert record.safe_id == new_locator
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, old_locator) is False
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, new_locator) is True


def test_two_distinct_locators_get_distinct_resource_ids():
    ledger = gate_runner.ResourceLedger()
    first_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, "/tmp/first.dump")
    second_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, "/tmp/second.dump")
    assert first_id != second_id


def test_get_by_locator_resolves_the_same_stable_record_as_get(tmp_path):
    ledger = gate_runner.ResourceLedger()
    locator = str(tmp_path / "workspace")
    resource_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, locator)

    by_locator = ledger.get_by_locator(gate_runner.ResourceKind.WORKSPACE, locator)
    assert by_locator is not None
    assert by_locator.resource_id == resource_id
    assert by_locator is ledger.get(resource_id)


def test_get_by_locator_returns_none_for_unknown_locator():
    ledger = gate_runner.ResourceLedger()
    assert ledger.get_by_locator(gate_runner.ResourceKind.WORKSPACE, "/never/acquired") is None


# --- Section 32: stable parent IDs -------------------------------------------


def test_parent_relationship_uses_resource_ids(tmp_path):
    ledger = gate_runner.ResourceLedger()
    workspace = str(tmp_path / "workspace")
    dump = str(tmp_path / "workspace" / "x.dump")

    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, workspace)
    child_id = ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        dump,
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=workspace,
    )

    assert ledger.get(child_id).parent_resource_id == parent_id


def test_relocating_parent_does_not_invalidate_stable_child_relationship(tmp_path):
    """Section 20: parent relocation must not change the parent's own
    resource_id, so a child's parent_resource_id (captured once, at
    child acquisition) remains valid even after the parent's locator
    changes."""
    ledger = gate_runner.ResourceLedger()
    old_workspace = str(tmp_path / "workspace")
    new_workspace = str(tmp_path / "workspace-renamed")
    dump = str(tmp_path / "workspace" / "x.dump")

    parent_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, old_workspace)
    child_id = ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        dump,
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=old_workspace,
    )

    ledger.relocate(gate_runner.ResourceKind.WORKSPACE, old_workspace, new_workspace)

    assert ledger.get(parent_id).resource_id == parent_id
    assert ledger.get(child_id).parent_resource_id == parent_id


# --- Section 33: duplicate acquisition semantics -----------------------------


def test_duplicate_acquisition_of_same_active_locator_is_rejected(tmp_path):
    ledger = gate_runner.ResourceLedger()
    locator = str(tmp_path / "workspace")
    other_parent = str(tmp_path / "other-parent")
    ledger.acquire(gate_runner.ResourceKind.WORKSPACE, locator)

    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.acquire(
            gate_runner.ResourceKind.WORKSPACE,
            locator,
            parent_kind=gate_runner.ResourceKind.OUTER_ROOT,
            parent_safe_id=other_parent,
        )


def test_duplicate_acquisition_rejection_never_silently_updates_stale_parent(tmp_path):
    ledger = gate_runner.ResourceLedger()
    locator = str(tmp_path / "workspace")
    original_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, locator)

    with pytest.raises(gate_runner.ResourceOwnershipError):
        ledger.acquire(
            gate_runner.ResourceKind.WORKSPACE,
            locator,
            parent_kind=gate_runner.ResourceKind.OUTER_ROOT,
            parent_safe_id=str(tmp_path / "someone-elses-root"),
        )

    record = ledger.get(original_id)
    assert record.parent_resource_id is None
    assert record.resource_id == original_id


def test_reacquiring_after_release_is_allowed_and_gets_a_fresh_id(tmp_path):
    """Section 18: the rejection only applies to an ACTIVE resource --
    once released, the same locator/kind is free to be acquired again
    (as a genuinely new resource, with its own new resource_id)."""
    ledger = gate_runner.ResourceLedger()
    locator = str(tmp_path / "workspace")
    first_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, locator)
    ledger.release(gate_runner.ResourceKind.WORKSPACE, locator)

    second_id = ledger.acquire(gate_runner.ResourceKind.WORKSPACE, locator)
    assert second_id != first_id
    assert ledger.get(first_id) is None
    assert ledger.get(second_id).resource_id == second_id


# --- Section 34: partial dump stable ID --------------------------------------


def test_partial_producer_failure_preserves_the_same_resource_id(tmp_path):
    """Mirrors the real backup_restore_smoke() reservation-before-producer
    pattern: the resource_id assigned at reservation time must be the
    SAME one still owned after a producer fails partway through, with
    the locator unchanged unless explicitly relocated."""
    ledger = gate_runner.ResourceLedger()
    dump_path = str(tmp_path / "node.dump")
    workspace = str(tmp_path / "workspace")
    ledger.acquire(gate_runner.ResourceKind.WORKSPACE, workspace)

    reserved_id = ledger.acquire(
        gate_runner.ResourceKind.TEMP_DUMP,
        dump_path,
        parent_kind=gate_runner.ResourceKind.WORKSPACE,
        parent_safe_id=workspace,
    )

    class _PartialWriteFailure(Exception):
        pass

    try:
        # Simulates a real pg_dump-style producer that writes a partial
        # file before raising.
        with open(dump_path, "wb") as handle:
            handle.write(b"partial-bytes")
        raise _PartialWriteFailure("producer failed partway through")
    except _PartialWriteFailure:
        pass

    record = ledger.get(reserved_id)
    assert record is not None
    assert record.resource_id == reserved_id
    assert record.safe_id == dump_path
    assert ledger.contains(gate_runner.ResourceKind.TEMP_DUMP, dump_path) is True
    assert reserved_id in {ref.resource_id for ref in ledger.retained_execution_resources()}


# --- Section 35: same-prefix and relocation regressions (already covered by
# test_release_gate_corrective_v11_resource_ownership.py directly; here we
# additionally confirm stable-ID identity survives those same scenarios) ----


def test_same_prefix_sibling_keeps_its_own_stable_id_after_parent_removal(tmp_path):
    import shutil

    parent_dir = tmp_path / "run"
    parent_dir.mkdir()
    sibling_file = tmp_path / "run-outside" / "file.dump"
    sibling_file.parent.mkdir()
    sibling_file.write_bytes(b"data")

    ledger = gate_runner.ResourceLedger()
    ledger.acquire(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))
    sibling_id = ledger.acquire(gate_runner.ResourceKind.TEMP_DUMP, str(sibling_file))

    shutil.rmtree(parent_dir)
    ledger.reconcile_verified_parent_removal(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))
    ledger.release(gate_runner.ResourceKind.WORKSPACE, str(parent_dir))

    assert ledger.get(sibling_id) is not None
    assert ledger.get(sibling_id).resource_id == sibling_id
