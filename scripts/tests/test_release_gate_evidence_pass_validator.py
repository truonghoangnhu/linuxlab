"""Task 15 Task 11 corrective v3, Section 3/16/28.6: PASS is not a
default state. `validate_for_pass()` is the ONE function allowed to
certify an EvidenceV1 candidate as PASS-worthy -- syntax/type validity
alone (corrective v2) is insufficient, since a proof can be well-typed
while semantically demonstrating failure (e.g. `signature_verified:
false`) and the runner must still refuse PASS.

These tests construct EvidenceV1 candidates directly (never via the
full orchestration pipeline) so every individual mandatory invariant
can be probed in isolation, per Section 28.6.
"""

from scripts.release_gate.environment import Authority
from scripts.release_gate.evidence import (
    REQUIRED_SCENARIO_ASSERTIONS,
    REQUIRED_SCENARIOS,
    SCHEMA_VERSION,
    PassDecision,
    ReleaseEvidence,
    validate_for_pass,
)

_GOOD_AUTHORITY = Authority(authoritative=True, platform="linux", reason=None).__dict__
_GOOD_ENVIRONMENT = {
    "platform": "linux",
    "python_version": "3.12.14",
    "django_version": "5.2.17",
    "postgres_version": "180001",
}
_GOOD_PROFILES = {"central": "linuxlab.settings.central", "node": "linuxlab.settings.node"}
_GOOD_VALIDATION = {
    "clean_tree": True,
    "central_check": "ok",
    "node_check": "ok",
    "central_migration_drift": "none",
    "node_migration_drift": "none",
}
_GOOD_PROOFS = {
    "package_sha256": "a" * 64,
    "payload_sha256": "b" * 64,
    "public_key_fingerprint": "c" * 64,
    "signature": {"verified": True},
    "outbox_to_inbox": {
        "event_uuid": "12345678-1234-5678-1234-567812345678",
        "inbox_count": 1,
        "node_state": "DELIVERED",
        "duplicate_idempotent": True,
    },
}
_GOOD_RETENTION = {
    "keep_on_failure_requested": False,
    "execution_resources_retained": False,
    "sanitized_debug_bundle_retained": True,
}


def _good_scenario(name):
    assertions = {key: True for key in REQUIRED_SCENARIO_ASSERTIONS[name]}
    # Fix up the non-bool-True expected values so this fixture is a
    # genuinely PASS-eligible baseline, not merely well-typed.
    if "final_inbox_count" in assertions:
        assertions["final_inbox_count"] = 1
    if "final_node_state" in assertions:
        assertions["final_node_state"] = "DELIVERED"
    if "central_inbox_mutated" in assertions:
        assertions["central_inbox_mutated"] = False
    return {"name": name, "status": "PASS", "assertions": assertions, "duration_ms": 1.0, "detail": ""}


def _good_scenarios():
    return {name: _good_scenario(name) for name in REQUIRED_SCENARIOS}


def _make_candidate(**overrides):
    fields = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": "rgoodrun00001",
            "git_sha": "a" * 40,
            "started_at": "2026-09-04T00:00:00+00:00",
            "finished_at": "2026-09-04T00:05:00+00:00",
            "authority": dict(_GOOD_AUTHORITY),
        },
        "environment": dict(_GOOD_ENVIRONMENT),
        "profiles": dict(_GOOD_PROFILES),
        "validation": dict(_GOOD_VALIDATION),
        "scenarios": _good_scenarios(),
        "proofs": dict(_GOOD_PROOFS),
        "retention": dict(_GOOD_RETENTION),
        "primary_failure": None,
        "cleanup_failure": None,
        "finalization_failure": None,
        "cleanup_status": "clean",
        "overall": "PASS",
        "exit_code": 0,
    }
    fields.update(overrides)
    return ReleaseEvidence(**fields)


def test_required_scenario_set_has_exactly_seven_entries():
    assert len(REQUIRED_SCENARIOS) == 7


def test_a_fully_well_formed_candidate_is_pass_eligible():
    decision = validate_for_pass(_make_candidate())
    assert isinstance(decision, PassDecision)
    assert decision.eligible is True
    assert decision.reasons == ()


# --- Section 28.6: PASS-validator rejection tests --------------------------


def test_rejects_candidate_missing_a_required_scenario():
    scenarios = _good_scenarios()
    del scenarios[REQUIRED_SCENARIOS[0]]
    decision = validate_for_pass(_make_candidate(scenarios=scenarios))
    assert decision.eligible is False
    assert decision.reasons


def test_rejects_candidate_with_an_extra_unrecognized_scenario():
    scenarios = _good_scenarios()
    scenarios["not_a_real_scenario"] = _good_scenario(REQUIRED_SCENARIOS[0])
    decision = validate_for_pass(_make_candidate(scenarios=scenarios))
    assert decision.eligible is False


def test_rejects_candidate_missing_a_proof():
    proofs = dict(_GOOD_PROOFS)
    del proofs["package_sha256"]
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False
    assert any("package_sha256" in reason for reason in decision.reasons)


def test_rejects_candidate_with_unverified_signature():
    proofs = dict(_GOOD_PROOFS)
    proofs["signature"] = {"verified": False}
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_candidate_with_inbox_count_not_one():
    proofs = dict(_GOOD_PROOFS)
    proofs["outbox_to_inbox"] = {**_GOOD_PROOFS["outbox_to_inbox"], "inbox_count": 0}
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_candidate_with_node_state_pending():
    proofs = dict(_GOOD_PROOFS)
    proofs["outbox_to_inbox"] = {**_GOOD_PROOFS["outbox_to_inbox"], "node_state": "PENDING"}
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_candidate_with_duplicate_idempotent_false():
    proofs = dict(_GOOD_PROOFS)
    proofs["outbox_to_inbox"] = {**_GOOD_PROOFS["outbox_to_inbox"], "duplicate_idempotent": False}
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_candidate_missing_outbox_proof():
    proofs = dict(_GOOD_PROOFS)
    del proofs["outbox_to_inbox"]
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_candidate_with_postgres_version_unknown():
    environment = dict(_GOOD_ENVIRONMENT)
    environment["postgres_version"] = "unknown"
    decision = validate_for_pass(_make_candidate(environment=environment))
    assert decision.eligible is False


def test_rejects_candidate_with_missing_profile():
    profiles = {"central": "linuxlab.settings.central"}
    decision = validate_for_pass(_make_candidate(profiles=profiles))
    assert decision.eligible is False


def test_rejects_candidate_with_failed_validation_check():
    validation = {**_GOOD_VALIDATION, "central_check": "failed"}
    decision = validate_for_pass(_make_candidate(validation=validation))
    assert decision.eligible is False


def test_rejects_candidate_with_migration_drift_detected():
    validation = {**_GOOD_VALIDATION, "node_migration_drift": "detected"}
    decision = validate_for_pass(_make_candidate(validation=validation))
    assert decision.eligible is False


def test_rejects_candidate_with_execution_resources_retained_true():
    retention = {**_GOOD_RETENTION, "execution_resources_retained": True}
    decision = validate_for_pass(_make_candidate(retention=retention))
    assert decision.eligible is False


def test_rejects_candidate_with_cleanup_status_not_clean():
    decision = validate_for_pass(_make_candidate(cleanup_status="failed"))
    assert decision.eligible is False


def test_rejects_candidate_with_a_primary_failure_present():
    from scripts.release_gate.evidence import FailureRecord

    record = FailureRecord(category="SCENARIO_FAILURE", stage="n1", code="AssertionError", safe_message="x")
    decision = validate_for_pass(_make_candidate(primary_failure=record.to_dict()))
    assert decision.eligible is False


def test_rejects_candidate_with_a_cleanup_failure_present():
    from scripts.release_gate.evidence import FailureRecord

    record = FailureRecord(category="CLEANUP_FAILURE", stage="cleanup", code="RuntimeError", safe_message="x")
    decision = validate_for_pass(_make_candidate(cleanup_failure=record.to_dict()))
    assert decision.eligible is False


def test_rejects_candidate_with_a_finalization_failure_present():
    from scripts.release_gate.evidence import FailureRecord

    record = FailureRecord(category="CLEANUP_FAILURE", stage="finalize", code="SecretLeakError", safe_message="x")
    decision = validate_for_pass(_make_candidate(finalization_failure=record.to_dict()))
    assert decision.eligible is False


def test_rejects_darwin_dev_candidate_with_authoritative_true():
    """Authority-consistency: a non-Linux platform reporting
    authoritative=True (i.e. lying about its own authority) must never
    be PASS-eligible -- this is an internally inconsistent candidate,
    not a legitimate development override."""
    authority = {"authoritative": True, "platform": "darwin", "reason": None}
    run = {**_make_candidate().run, "authority": authority}
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


def test_accepts_darwin_dev_candidate_with_consistent_non_authoritative_authority():
    authority = {"authoritative": False, "platform": "darwin", "reason": "development_only_platform"}
    run = {**_make_candidate().run, "authority": authority}
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is True


def test_rejects_scenario_with_missing_required_assertion_key():
    scenarios = _good_scenarios()
    name = REQUIRED_SCENARIOS[1]
    assertions = dict(scenarios[name]["assertions"])
    first_key = next(iter(assertions))
    del assertions[first_key]
    scenarios[name] = {**scenarios[name], "assertions": assertions}
    decision = validate_for_pass(_make_candidate(scenarios=scenarios))
    assert decision.eligible is False


def test_rejects_scenario_with_an_extra_unrecognized_assertion_key():
    scenarios = _good_scenarios()
    name = REQUIRED_SCENARIOS[1]
    assertions = {**scenarios[name]["assertions"], "not_a_required_key": True}
    scenarios[name] = {**scenarios[name], "assertions": assertions}
    decision = validate_for_pass(_make_candidate(scenarios=scenarios))
    assert decision.eligible is False


def test_rejects_scenario_with_a_false_required_assertion():
    scenarios = _good_scenarios()
    name = REQUIRED_SCENARIOS[1]
    assertions = dict(scenarios[name]["assertions"])
    first_key = next(iter(assertions))
    assertions[first_key] = False
    scenarios[name] = {**scenarios[name], "assertions": assertions}
    decision = validate_for_pass(_make_candidate(scenarios=scenarios))
    assert decision.eligible is False


def test_rejects_scenario_that_did_not_pass_even_with_correct_assertions():
    scenarios = _good_scenarios()
    name = REQUIRED_SCENARIOS[1]
    scenarios[name] = {**scenarios[name], "status": "FAIL"}
    decision = validate_for_pass(_make_candidate(scenarios=scenarios))
    assert decision.eligible is False


def test_reasons_never_reflect_raw_scenario_or_proof_values():
    """Safety of the decision output itself: reasons are fixed short
    codes, never the actual (potentially sensitive/arbitrary) values
    that failed validation."""
    proofs = dict(_GOOD_PROOFS)
    proofs["package_sha256"] = "sentinel-answer-key-like-value"
    # This candidate is malformed at the syntax layer, which normally
    # would already have been rejected by build_typed_proofs() upstream
    # -- but validate_for_pass() must still never reflect a raw stored
    # value even if one somehow reached it.
    candidate = _make_candidate(proofs=proofs)
    decision = validate_for_pass(candidate)
    assert decision.eligible is True or "sentinel-answer-key-like-value" not in " ".join(decision.reasons)
