"""Task 15 Task 11 corrective v2, Section 8 ("Typed ScenarioEvidence"):
a scenario's raw returned detail dict must never become arbitrary
release-evidence content. Only a closed, per-key-validated assertion
registry may cross into ScenarioResult.assertions -- an unrecognized
key is silently dropped (it is simply not release-evidence content),
but a recognized key with a malformed value is rejected outright.
"""

import pytest

from scripts.release_gate.evidence import InvalidProofError, build_typed_assertions


def test_build_typed_assertions_accepts_the_n1_example_set():
    detail = {
        "existing_active_preserved": True,
        "active_package_identity_unchanged": True,
        "replacement_rejected": True,
        "payload_sha256": "a" * 64,  # a proof field, not an assertion -- ignored here
    }
    assertions = build_typed_assertions(detail)
    assert assertions == {
        "existing_active_preserved": True,
        "active_package_identity_unchanged": True,
        "replacement_rejected": True,
    }


def test_build_typed_assertions_accepts_the_n3_example_set():
    detail = {
        "central_commit_before_ack_loss": True,
        "node_pending_before_retry": True,
        "final_inbox_count": 1,
        "final_node_state": "DELIVERED",
        "duplicate_idempotent": True,
    }
    assertions = build_typed_assertions(detail)
    assert assertions == detail


def test_build_typed_assertions_ignores_unknown_keys():
    assertions = build_typed_assertions({"event_id": "abc-123", "existing_active_preserved": True})
    assert assertions == {"existing_active_preserved": True}
    assert "event_id" not in assertions


def test_build_typed_assertions_returns_empty_for_non_dict_input():
    assert build_typed_assertions(None) == {}
    assert build_typed_assertions("not a dict") == {}


@pytest.mark.parametrize(
    "key,bad_value",
    [
        ("existing_active_preserved", "true"),
        ("replacement_rejected", 1),
        ("final_inbox_count", -1),
        ("final_inbox_count", "1"),
        ("final_node_state", "delivered"),
        ("final_node_state", "SENT"),
        ("duplicate_idempotent", "yes"),
    ],
)
def test_build_typed_assertions_rejects_malformed_known_key_values(key, bad_value):
    with pytest.raises(InvalidProofError):
        build_typed_assertions({key: bad_value})
