"""Task 15 Task 11 corrective v9, Gap 3 (design spec Sections 24-29):
canonical run-ID validation was incomplete and had a live newline-
bypass bug.

`environment._RUN_ID_PATTERN` used a permissive trailing `$` with
`.match()` -- Python's `$` also matches immediately before a single
trailing "\\n", and `.match()` doesn't require the whole string to be
consumed anyway, so `assert_valid_run_id("rvalid\\n")` did NOT raise
before this corrective. Separately, `evidence._validate_for_pass_
metadata()` never applied the canonical run-ID grammar at all -- only
a generic non-empty-string check -- so a hand-constructed candidate
carrying `run["run_id"] = "../bad"` passed `validate_for_pass()`.

These are the mandatory RED tests from the v9 design spec, Section 35,
items 13-15.
"""

import pytest

from scripts.release_gate.environment import (
    PreflightError,
    assert_valid_run_id,
    generate_run_id,
    validate_canonical_run_id,
)
from scripts.release_gate.evidence import SCHEMA_VERSION, ReleaseEvidence, validate_for_pass

_MALFORMED_RUN_IDS = [
    "rvalid\n",
    "r valid",
    "../bad",
    "path/bad",
    "",
]


class _StrSubclass(str):
    """A str subclass -- `isinstance(x, str)` is True, but
    `type(x) is str` is False. The canonical validator must reject it
    even though it otherwise looks like a well-formed run_id."""


def _make_candidate(**overrides):
    fields = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": "rgoodrunid001",
            "git_sha": "a" * 40,
            "started_at": "2026-09-06T00:00:00+00:00",
            "finished_at": "2026-09-06T00:05:00+00:00",
            "authority": {"authoritative": True, "platform": "linux", "reason": None},
        },
        "environment": {
            "platform": "linux",
            "python_version": "3.12.0",
            "django_version": "5.2",
            "postgres_version": "180000",
        },
        "profiles": {"central": "linuxlab.settings.central", "node": "linuxlab.settings.node"},
        "validation": {
            "clean_tree": True,
            "central_check": "ok",
            "node_check": "ok",
            "central_migration_drift": "none",
            "node_migration_drift": "none",
        },
        "scenarios": {},
        "proofs": {},
        "retention": {
            "keep_on_failure_requested": False,
            "execution_resources_retained": False,
            "sanitized_debug_bundle_retained": True,
        },
        "primary_failure": None,
        "cleanup_failure": None,
        "finalization_failure": None,
        "cleanup_status": "clean",
        "overall": "PASS",
        "exit_code": 0,
    }
    fields.update(overrides)
    return ReleaseEvidence(**fields)


# --- RED test 13: builder boundary rejects malformed run IDs ---------------


@pytest.mark.parametrize("bad_run_id", _MALFORMED_RUN_IDS)
def test_assert_valid_run_id_rejects_malformed_values(bad_run_id):
    with pytest.raises(PreflightError):
        assert_valid_run_id(bad_run_id)


def test_assert_valid_run_id_rejects_str_subclass():
    with pytest.raises(PreflightError):
        assert_valid_run_id(_StrSubclass("rgoodrunid001"))


@pytest.mark.parametrize("bad_run_id", _MALFORMED_RUN_IDS)
def test_validate_canonical_run_id_pure_function_rejects_malformed_values(bad_run_id):
    assert validate_canonical_run_id(bad_run_id) is False


def test_validate_canonical_run_id_pure_function_rejects_str_subclass():
    assert validate_canonical_run_id(_StrSubclass("rgoodrunid001")) is False


def test_validate_canonical_run_id_never_raises_on_non_str_input():
    assert validate_canonical_run_id(None) is False
    assert validate_canonical_run_id(True) is False
    assert validate_canonical_run_id(12345) is False


# --- RED test 14: final validator rejects the same malformed values in ----
# --- a bypassed candidate ----------------------------------------------------


@pytest.mark.parametrize("bad_run_id", _MALFORMED_RUN_IDS)
def test_validate_for_pass_rejects_bypassed_candidate_with_malformed_run_id(bad_run_id):
    run = dict(_make_candidate().run)
    run["run_id"] = bad_run_id
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


def test_validate_for_pass_rejects_bypassed_candidate_with_str_subclass_run_id():
    run = dict(_make_candidate().run)
    run["run_id"] = _StrSubclass("rgoodrunid001")
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


def test_validate_for_pass_reports_run_id_malformed_reason_for_path_traversal():
    """Confirms the fix actually engages the canonical-grammar check
    (not merely rejecting for an unrelated reason) -- a syntactically
    non-empty, non-"unknown" string like "../bad" must be flagged with
    the dedicated malformed-grammar reason code."""
    run = dict(_make_candidate().run)
    run["run_id"] = "../bad"
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False
    assert "run_id_malformed" in decision.reasons


# --- RED test 15: existing valid run IDs still pass both boundaries -------


def test_generated_run_ids_still_pass_both_boundaries():
    for _ in range(20):
        run_id = generate_run_id()
        assert_valid_run_id(run_id)  # does not raise
        assert validate_canonical_run_id(run_id) is True

        # This fixture's `scenarios`/`proofs` are deliberately minimal
        # (not a full PASS-worthy candidate) -- only the run_id-specific
        # reason codes are asserted here, never overall eligibility.
        run = dict(_make_candidate().run)
        run["run_id"] = run_id
        decision = validate_for_pass(_make_candidate(run=run))
        assert "run_id_missing" not in decision.reasons
        assert "run_id_malformed" not in decision.reasons


@pytest.mark.parametrize(
    "existing_valid_run_id",
    [
        "rgoodrun00001",
        "rfaketest0001",
        "rabc123456789",
        "rexplicit0001",
        "rpub00000001",
    ],
)
def test_existing_test_suite_literal_run_ids_still_pass_both_boundaries(existing_valid_run_id):
    assert_valid_run_id(existing_valid_run_id)  # does not raise
    assert validate_canonical_run_id(existing_valid_run_id) is True

    # Same minimal-fixture caveat as above: only the run_id-specific
    # reason codes are asserted, never overall eligibility.
    run = dict(_make_candidate().run)
    run["run_id"] = existing_valid_run_id
    decision = validate_for_pass(_make_candidate(run=run))
    assert "run_id_missing" not in decision.reasons
    assert "run_id_malformed" not in decision.reasons
