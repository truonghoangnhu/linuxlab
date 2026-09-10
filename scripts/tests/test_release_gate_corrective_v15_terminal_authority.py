"""Task 15 Task 11 corrective v15 (bounded closure), Section 2: closes
the independent v14 review's GateRunResult authority-exclusivity gap.

`GateRunResult` used to validate PASS-path coherence between `evidence`
and `terminal_failure`, but never rejected a NON-PASS result carrying
BOTH an ordinary `ReleaseEvidence` (evidence mode) AND an emergency
`terminal_failure` dict (emergency mode) at once -- two contradictory
failure authorities could coexist on the same result, including with
outright conflicting failure categories.

These are the mandatory RED cases from the v15 corrective prompt,
Section 2A.
"""

import pytest

from scripts.release_gate.evidence import (
    REQUIRED_SCENARIO_ASSERTIONS,
    REQUIRED_SCENARIOS,
    SCHEMA_VERSION,
    ReleaseEvidence,
    UnsupportedEvidenceValueError,
)
from scripts.release_gate.gate_runner import GateRunResult, Stage

_VALID_EVENT_UUID = "12345678-1234-5678-1234-567812345678"

_PASS_WORTHY_PROOFS = {
    "package_sha256": "a" * 64,
    "payload_sha256": "b" * 64,
    "public_key_fingerprint": "c" * 64,
    "signature": {"verified": True},
    "outbox_to_inbox": {
        "event_uuid": _VALID_EVENT_UUID,
        "inbox_count": 1,
        "node_state": "DELIVERED",
        "duplicate_idempotent": True,
    },
}


def _pass_worthy_assertions_for(name):
    assertions = {}
    for key in REQUIRED_SCENARIO_ASSERTIONS[name]:
        if key == "final_inbox_count":
            assertions[key] = 1
        elif key == "final_node_state":
            assertions[key] = "DELIVERED"
        elif key == "central_inbox_mutated":
            assertions[key] = False
        else:
            assertions[key] = True
    return assertions


def _pass_worthy_scenarios() -> dict:
    return {
        name: {
            "name": name,
            "status": "PASS",
            "assertions": _pass_worthy_assertions_for(name),
            "duration_ms": 1.0,
            "detail": "",
        }
        for name in REQUIRED_SCENARIOS
    }


def _valid_run(run_id="rv15termauth1") -> dict:
    return {
        "run_id": run_id,
        "git_sha": "a" * 40,
        "started_at": "2026-09-10T00:00:00+00:00",
        "finished_at": "2026-09-10T00:05:00+00:00",
        "authority": {"authoritative": True, "platform": "linux", "reason": None},
    }


def pass_eligible_evidence(**overrides) -> ReleaseEvidence:
    kwargs = {
        "schema_version": SCHEMA_VERSION,
        "run": _valid_run(),
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
        "scenarios": _pass_worthy_scenarios(),
        "proofs": dict(_PASS_WORTHY_PROOFS),
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
    kwargs.update(overrides)
    return ReleaseEvidence(**kwargs)


def scenario_failure_evidence(*, run_id="rv15termauth2") -> ReleaseEvidence:
    """A real, valid, non-PASS ReleaseEvidence -- a SCENARIO_FAILURE
    whose primary_failure.category is SCENARIO_FAILURE, exactly the
    kind of ordinary evidence-mode failure result the v15 prompt
    requires as the "real fixture" for the conflicting-category case."""
    return pass_eligible_evidence(
        run=_valid_run(run_id=run_id),
        overall="SCENARIO_FAILURE",
        exit_code=4,
        primary_failure={
            "category": "SCENARIO_FAILURE",
            "stage": "happy_path",
            "code": "AssertionError",
            "safe_message": "an internal failure occurred",
        },
    )


def valid_evidence_mode_failure_kwargs() -> dict:
    return {
        "overall": "SCENARIO_FAILURE",
        "exit_code": 4,
        "evidence": scenario_failure_evidence(),
        "evidence_dir": None,
        "json_path": None,
        "markdown_path": None,
        "terminal_failure": None,
        "stage": Stage.DONE,
    }


def valid_published_failure_kwargs(run_id="rv15termauth3") -> dict:
    """A genuinely PUBLISHED, non-PASS, evidence-mode failure -- exactly
    the real shape `_finalize()` produces when evidence publication
    succeeds even though the run itself did not overall PASS (Section
    2's `evidence_dir` MAY legitimately exist by itself on a
    publication-failure path" nuance's sibling: here publication fully
    succeeded). Used as the base for the json_path/markdown_path RED
    cases below: on pre-v15 code, `terminal_failure` was not validated
    at all outside the `overall == "PASS"` branch, so attaching it to
    this otherwise fully valid, already-published candidate was
    silently accepted -- the only way to demonstrate that gap without
    tripping an unrelated PRE-EXISTING check (json_path/markdown_path
    always requiring `evidence`/`evidence_dir` to already be present)."""
    evidence = scenario_failure_evidence(run_id=run_id)
    return {
        "overall": "SCENARIO_FAILURE",
        "exit_code": 4,
        "evidence": evidence,
        "evidence_dir": "/tmp/evidence-dir",
        "json_path": "/tmp/evidence-dir/release-evidence.json",
        "markdown_path": "/tmp/evidence-dir/release-evidence.md",
        "terminal_failure": None,
        "stage": Stage.DONE,
    }


def valid_emergency_mode_kwargs() -> dict:
    return {
        "overall": "STARTUP_FAILURE",
        "exit_code": 3,
        "evidence": None,
        "evidence_dir": None,
        "json_path": None,
        "markdown_path": None,
        "terminal_failure": {
            "category": "STARTUP_FAILURE",
            "stage": "finalize",
            "code": "RuntimeError",
            "safe_message": "an internal failure occurred",
        },
        "stage": Stage.DONE,
    }


def valid_pass_kwargs() -> dict:
    return {
        "overall": "PASS",
        "exit_code": 0,
        "evidence": pass_eligible_evidence(),
        "evidence_dir": "/tmp/evidence-dir",
        "json_path": "/tmp/evidence-dir/release-evidence.json",
        "markdown_path": "/tmp/evidence-dir/release-evidence.md",
        "terminal_failure": None,
        "stage": Stage.DONE,
    }


# --- Section 2A: mutual-exclusion RED cases ------------------------------


def test_published_failure_fixture_is_valid_without_terminal_failure():
    """Sanity baseline for the json_path/markdown_path RED cases below --
    proves `valid_published_failure_kwargs()` is a genuinely well-formed
    candidate on its own, so a failure in the tests that add
    `terminal_failure` to it is attributable ONLY to that addition."""
    result = GateRunResult(**valid_published_failure_kwargs())
    assert result.overall == "SCENARIO_FAILURE"
    assert result.json_path is not None


def test_evidence_and_terminal_failure_together_is_rejected():
    """General case: `overall`/`exit_code` are deliberately left matching
    `evidence.overall`/`evidence.exit_code` so the ONLY invariant this
    candidate violates is the evidence/terminal_failure co-presence --
    isolating it from the pre-existing (and still-required)
    overall-vs-evidence.overall agreement check."""
    kwargs = valid_evidence_mode_failure_kwargs()
    kwargs["terminal_failure"] = {
        "category": "CLEANUP_FAILURE",
        "stage": "cleanup",
        "code": "RuntimeError",
        "safe_message": "an internal failure occurred",
    }
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_conflicting_category_between_evidence_and_terminal_failure_is_rejected():
    """The exact conceptual invalid state from the v15 prompt: evidence's
    own primary_failure.category is SCENARIO_FAILURE while the attached
    terminal_failure.category claims STARTUP_FAILURE."""
    kwargs = valid_evidence_mode_failure_kwargs()
    assert kwargs["evidence"].primary_failure["category"] == "SCENARIO_FAILURE"
    kwargs["terminal_failure"] = {
        "category": "STARTUP_FAILURE",
        "stage": "startup",
        "code": "RuntimeError",
        "safe_message": "an internal failure occurred",
    }
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_terminal_failure_with_json_path_is_rejected():
    kwargs = valid_published_failure_kwargs(run_id="rv15termauth3a")
    kwargs["terminal_failure"] = {
        "category": "CLEANUP_FAILURE",
        "stage": "finalize_publish",
        "code": "RuntimeError",
        "safe_message": "an internal failure occurred",
    }
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_terminal_failure_with_markdown_path_is_rejected():
    kwargs = valid_published_failure_kwargs(run_id="rv15termauth3b")
    kwargs["terminal_failure"] = {
        "category": "CLEANUP_FAILURE",
        "stage": "finalize_publish",
        "code": "RuntimeError",
        "safe_message": "an internal failure occurred",
    }
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_terminal_failure_with_evidence_is_rejected_even_when_categories_agree():
    """Categories agreeing does not make the dual-authority state valid
    -- the two fields are mutually exclusive regardless of content."""
    kwargs = valid_evidence_mode_failure_kwargs()
    kwargs["terminal_failure"] = {
        "category": "SCENARIO_FAILURE",
        "stage": "happy_path",
        "code": "AssertionError",
        "safe_message": "an internal failure occurred",
    }
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


# --- Section 2A: legitimate shapes must still construct ------------------


def test_legitimate_evidence_based_failure_result_still_constructs():
    result = GateRunResult(**valid_evidence_mode_failure_kwargs())
    assert result.overall == "SCENARIO_FAILURE"
    assert result.evidence is not None
    assert result.terminal_failure is None


def test_legitimate_emergency_result_still_constructs():
    result = GateRunResult(**valid_emergency_mode_kwargs())
    assert result.overall == "STARTUP_FAILURE"
    assert result.evidence is None
    assert result.terminal_failure is not None


def test_legitimate_pass_result_still_constructs():
    result = GateRunResult(**valid_pass_kwargs())
    assert result.overall == "PASS"
    assert result.evidence is not None
    assert result.terminal_failure is None
