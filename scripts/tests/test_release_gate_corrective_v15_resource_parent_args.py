"""Task 15 Task 11 corrective v15 (bounded closure), Section 3: closes
the independent v14 review's `ResourceLedger.acquire()` one-sided
legacy-parent-argument gap.

`acquire()` used to accept `parent_kind` set with `parent_safe_id` None
(or vice versa) and silently create an UNPARENTED record while still
retaining half of the legacy tuple on the stored `ResourceRef` -- a
half-parented record that neither resolves a real parent nor honestly
reports "no parent". Exactly three input states are legitimate: both
absent (unparented), both present and resolvable (parented), or both
present but unresolvable (already rejected pre-v15). Exactly one
supplied is the missing fourth case this corrective closes.

These are the mandatory RED cases from the v15 corrective prompt,
Section 3A.
"""

import pytest

from scripts.release_gate.gate_runner import ResourceKind, ResourceLedger, ResourceOwnershipError


def test_parent_kind_without_parent_safe_id_is_rejected():
    ledger = ResourceLedger()
    with pytest.raises(ResourceOwnershipError):
        ledger.acquire(ResourceKind.TEMP_DUMP, "child", parent_kind=ResourceKind.WORKSPACE)

    assert ledger.is_empty() is True
    assert ledger.contains(ResourceKind.TEMP_DUMP, "child") is False
    assert ledger.all_retained_resources() == ()


def test_parent_safe_id_without_parent_kind_is_rejected():
    ledger = ResourceLedger()
    with pytest.raises(ResourceOwnershipError):
        ledger.acquire(ResourceKind.TEMP_DUMP, "child", parent_safe_id="/tmp/workspace")

    assert ledger.is_empty() is True
    assert ledger.contains(ResourceKind.TEMP_DUMP, "child") is False
    assert ledger.all_retained_resources() == ()


def test_one_sided_parent_kind_rejection_is_atomic_and_leaves_other_resources_untouched():
    ledger = ResourceLedger()
    unrelated_id = ledger.acquire(ResourceKind.WORKSPACE, "/tmp/unrelated")
    before = ledger.all_retained_resources()

    with pytest.raises(ResourceOwnershipError):
        ledger.acquire(ResourceKind.TEMP_DUMP, "child", parent_kind=ResourceKind.WORKSPACE)

    assert ledger.all_retained_resources() == before
    assert ledger.contains(ResourceKind.TEMP_DUMP, "child") is False
    assert ledger.get_by_locator(ResourceKind.TEMP_DUMP, "child") is None
    assert ledger.contains(ResourceKind.WORKSPACE, "/tmp/unrelated") is True
    assert ledger.get(unrelated_id) is not None


def test_one_sided_parent_safe_id_rejection_is_atomic_and_leaves_other_resources_untouched():
    ledger = ResourceLedger()
    unrelated_id = ledger.acquire(ResourceKind.WORKSPACE, "/tmp/unrelated")
    before = ledger.all_retained_resources()

    with pytest.raises(ResourceOwnershipError):
        ledger.acquire(ResourceKind.TEMP_DUMP, "child", parent_safe_id="/tmp/workspace")

    assert ledger.all_retained_resources() == before
    assert ledger.contains(ResourceKind.TEMP_DUMP, "child") is False
    assert ledger.get_by_locator(ResourceKind.TEMP_DUMP, "child") is None
    assert ledger.contains(ResourceKind.WORKSPACE, "/tmp/unrelated") is True
    assert ledger.get(unrelated_id) is not None


# --- both-absent / both-present legitimate shapes must still work --------


def test_both_absent_is_a_valid_unparented_acquisition():
    ledger = ResourceLedger()
    resource_id = ledger.acquire(ResourceKind.WORKSPACE, "/tmp/workspace")

    ref = ledger.get(resource_id)
    assert ref is not None
    assert ref.parent_resource_id is None
    assert ledger.contains(ResourceKind.WORKSPACE, "/tmp/workspace") is True


def test_both_supplied_and_resolvable_sets_parent_resource_id():
    ledger = ResourceLedger()
    parent_id = ledger.acquire(ResourceKind.WORKSPACE, "/tmp/workspace")
    child_id = ledger.acquire(
        ResourceKind.TEMP_DUMP,
        "/tmp/workspace/child.dump",
        parent_kind=ResourceKind.WORKSPACE,
        parent_safe_id="/tmp/workspace",
    )

    child_ref = ledger.get(child_id)
    assert child_ref is not None
    assert child_ref.parent_resource_id == parent_id


def test_both_supplied_but_unresolved_is_rejected_with_no_mutation():
    ledger = ResourceLedger()
    before = ledger.all_retained_resources()

    with pytest.raises(ResourceOwnershipError):
        ledger.acquire(
            ResourceKind.TEMP_DUMP,
            "/tmp/workspace/child.dump",
            parent_kind=ResourceKind.WORKSPACE,
            parent_safe_id="/tmp/does-not-exist",
        )

    assert ledger.all_retained_resources() == before
    assert ledger.contains(ResourceKind.TEMP_DUMP, "/tmp/workspace/child.dump") is False
