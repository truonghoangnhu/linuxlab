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

import pytest

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
    candidate = _make_candidate(proofs=proofs)
    decision = validate_for_pass(candidate)
    assert decision.eligible is False
    assert "sentinel-answer-key-like-value" not in " ".join(decision.reasons)


# --- Task 15 Task 11 corrective v4, Section 8/12: validate_for_pass() is
# the AUTHORITATIVE validation boundary -- it must independently
# re-validate proof/assertion format and type, never merely trust that
# an already-present value was constructed through build_typed_proofs()/
# build_typed_assertions() upstream. These tests bypass those builders
# entirely, constructing a candidate directly with a malformed value
# already sitting in `proofs`/`scenarios[...]["assertions"]`, exactly as
# a bug elsewhere (or a hand-built candidate) might.


def test_rejects_a_directly_constructed_candidate_with_malformed_package_sha256():
    proofs = dict(_GOOD_PROOFS)
    proofs["package_sha256"] = "sentinel-answer-key-like-value"
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_a_directly_constructed_candidate_with_uppercase_hash():
    proofs = dict(_GOOD_PROOFS)
    proofs["payload_sha256"] = "B" * 64
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_a_directly_constructed_candidate_with_short_hash():
    proofs = dict(_GOOD_PROOFS)
    proofs["public_key_fingerprint"] = "c" * 63
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_a_directly_constructed_candidate_with_non_string_hash():
    proofs = dict(_GOOD_PROOFS)
    proofs["package_sha256"] = 12345
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_a_directly_constructed_candidate_with_integer_1_as_signature_verified():
    """Section 18: 1 == True in Python -- a strict type check must
    reject the integer 1 as a substitute for the boolean True, even
    though a naive equality check would accept it."""
    proofs = dict(_GOOD_PROOFS)
    proofs["signature"] = {"verified": 1}
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_a_directly_constructed_candidate_with_string_true_as_signature_verified():
    proofs = dict(_GOOD_PROOFS)
    proofs["signature"] = {"verified": "true"}
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_a_directly_constructed_candidate_with_bool_as_inbox_count():
    """Section 14.2: bool is an int subclass in Python -- True/False
    must never satisfy a strict int field."""
    proofs = dict(_GOOD_PROOFS)
    proofs["outbox_to_inbox"] = {**_GOOD_PROOFS["outbox_to_inbox"], "inbox_count": True}
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_a_directly_constructed_candidate_with_extra_outbox_key():
    proofs = dict(_GOOD_PROOFS)
    proofs["outbox_to_inbox"] = {**_GOOD_PROOFS["outbox_to_inbox"], "extra_field": "x"}
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_a_directly_constructed_candidate_with_invalid_event_uuid():
    proofs = dict(_GOOD_PROOFS)
    proofs["outbox_to_inbox"] = {**_GOOD_PROOFS["outbox_to_inbox"], "event_uuid": "not-a-uuid"}
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_a_directly_constructed_candidate_with_git_sha_wrong_length():
    run = dict(_make_candidate().run)
    run["git_sha"] = "abc123"
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


def test_rejects_a_directly_constructed_candidate_with_git_sha_uppercase():
    run = dict(_make_candidate().run)
    run["git_sha"] = "A" * 40
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


def test_rejects_scenario_assertion_integer_1_in_place_of_bool_true():
    """Section 18/21: an assertion value of literal int 1 must not
    satisfy a required bool-True assertion, even though 1 == True."""
    scenarios = _good_scenarios()
    name = REQUIRED_SCENARIOS[1]
    assertions = dict(scenarios[name]["assertions"])
    first_key = next(iter(assertions))
    assertions[first_key] = 1
    scenarios[name] = {**scenarios[name], "assertions": assertions}
    decision = validate_for_pass(_make_candidate(scenarios=scenarios))
    assert decision.eligible is False


def test_rejects_scenario_assertion_integer_0_in_place_of_bool_false():
    """For an expected-False assertion (central_inbox_mutated), integer
    0 must not satisfy it even though 0 == False."""
    scenarios = _good_scenarios()
    name = "n2_revoked_credential"
    assertions = dict(scenarios[name]["assertions"])
    assertions["central_inbox_mutated"] = 0
    scenarios[name] = {**scenarios[name], "assertions": assertions}
    decision = validate_for_pass(_make_candidate(scenarios=scenarios))
    assert decision.eligible is False


# --- Task 15 Task 11 corrective v5, Section 3: strict whole-string hash/
# Git-SHA validation -- a trailing newline/space must never be accepted
# by validate_for_pass() any more than by validate_sha256_hex() itself.


def test_rejects_candidate_with_package_sha256_trailing_newline():
    proofs = dict(_GOOD_PROOFS)
    proofs["package_sha256"] = "a" * 64 + "\n"
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_candidate_with_payload_sha256_trailing_space():
    proofs = dict(_GOOD_PROOFS)
    proofs["payload_sha256"] = "b" * 64 + " "
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_candidate_with_git_sha_trailing_newline():
    run = dict(_make_candidate().run)
    run["git_sha"] = "a" * 40 + "\n"
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


def test_rejects_candidate_with_git_sha_trailing_space():
    run = dict(_make_candidate().run)
    run["git_sha"] = "a" * 40 + " "
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


# --- Task 15 Task 11 corrective v5, Section 3: strict timestamps -----------


@pytest.mark.parametrize(
    "bad_value", [True, False, 12345, None, "", "not-a-timestamp", "2026-09-04", "2026-09-04T00:00:00+00:00\n"]
)
def test_rejects_candidate_with_malformed_started_at(bad_value):
    run = dict(_make_candidate().run)
    run["started_at"] = bad_value
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


@pytest.mark.parametrize("bad_value", [True, False, 12345, None, "", "not-a-timestamp"])
def test_rejects_candidate_with_malformed_finished_at(bad_value):
    run = dict(_make_candidate().run)
    run["finished_at"] = bad_value
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


# --- Task 15 Task 11 corrective v5, Section 3: strict required string
# metadata -- a bool/int must never satisfy a required non-empty-string
# field just because it is truthy (e.g. `not True` is falsy, so a naive
# `not value` check wrongly admits `True`).


@pytest.mark.parametrize("bad_value", [True, False, 12345, None, "", "unknown"])
def test_rejects_candidate_with_malformed_environment_platform(bad_value):
    environment = dict(_GOOD_ENVIRONMENT)
    environment["platform"] = bad_value
    decision = validate_for_pass(_make_candidate(environment=environment))
    assert decision.eligible is False


@pytest.mark.parametrize("bad_value", [True, False, 12345, None, "", "unknown"])
def test_rejects_candidate_with_malformed_python_version(bad_value):
    environment = dict(_GOOD_ENVIRONMENT)
    environment["python_version"] = bad_value
    decision = validate_for_pass(_make_candidate(environment=environment))
    assert decision.eligible is False


@pytest.mark.parametrize("bad_value", [True, False, 12345, None, "", "unknown"])
def test_rejects_candidate_with_malformed_django_version(bad_value):
    environment = dict(_GOOD_ENVIRONMENT)
    environment["django_version"] = bad_value
    decision = validate_for_pass(_make_candidate(environment=environment))
    assert decision.eligible is False


@pytest.mark.parametrize("bad_value", [True, False, 12345, None, ""])
def test_rejects_candidate_with_malformed_central_profile(bad_value):
    profiles = {"central": bad_value, "node": "linuxlab.settings.node"}
    decision = validate_for_pass(_make_candidate(profiles=profiles))
    assert decision.eligible is False


@pytest.mark.parametrize("bad_value", [True, False, 12345, None, ""])
def test_rejects_candidate_with_malformed_node_profile(bad_value):
    profiles = {"central": "linuxlab.settings.central", "node": bad_value}
    decision = validate_for_pass(_make_candidate(profiles=profiles))
    assert decision.eligible is False


# --- Task 15 Task 11 corrective v6, Section 7: stricter scalar identity
# checks -- schema_version/run_id/authority.platform must reject a bool
# substitute, since `True == 1` and `not True` is falsy, so naive
# equality/truthy checks would wrongly admit `True`.


def test_rejects_candidate_with_schema_version_true():
    """`True == 1` in Python, so a naive `!=` comparison against
    SCHEMA_VERSION (1) would wrongly accept the bool True as a valid
    schema_version."""
    decision = validate_for_pass(_make_candidate(schema_version=True))
    assert decision.eligible is False


@pytest.mark.parametrize("bad_value", [True, False, 12345, None, "", "unknown"])
def test_rejects_candidate_with_malformed_run_id(bad_value):
    run = dict(_make_candidate().run)
    run["run_id"] = bad_value
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


@pytest.mark.parametrize("bad_value", [True, False, 12345, None, ""])
def test_rejects_candidate_with_malformed_authority_platform(bad_value):
    """authority.platform must be validated as a strict non-empty string
    in its own right, not merely inferred from the authoritative/reason
    branches -- e.g. a non-authoritative candidate with
    authority.platform=True and a correct `reason` must still be
    rejected."""
    run = dict(_make_candidate().run)
    run["authority"] = {"authoritative": False, "platform": bad_value, "reason": "development_only_platform"}
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


# --- Task 15 Task 11 corrective v6, Section 7: semantic (not
# format-only-regex) ISO-8601 timestamp validation -- must reject
# calendar-impossible dates/times and out-of-range UTC offsets that a
# format-only regex would wrongly accept.


@pytest.mark.parametrize(
    "bad_value",
    [
        "2026-02-30T00:00:00+00:00",  # February 30th does not exist
        "2026-01-01T25:00:00+00:00",  # hour 25 is impossible
        "2026-01-01T00:61:00+00:00",  # minute 61 is impossible
        "2026-01-01T00:00:00+24:00",  # +24:00 offset is out of range
        "2026-13-01T00:00:00+00:00",  # month 13 does not exist
    ],
)
def test_rejects_candidate_with_calendar_impossible_started_at(bad_value):
    run = dict(_make_candidate().run)
    run["started_at"] = bad_value
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


def test_rejects_candidate_with_timezone_naive_started_at():
    """A parseable but timezone-NAIVE timestamp must still be rejected --
    the spec requires timezone-aware ISO-8601 datetimes."""
    run = dict(_make_candidate().run)
    run["started_at"] = "2026-09-04T00:00:00"
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


def test_accepts_candidate_with_z_suffix_timestamp():
    """A "Z"-suffixed (Zulu/UTC) timestamp is valid, timezone-aware
    ISO-8601 and must be accepted."""
    run = dict(_make_candidate().run)
    run["started_at"] = "2026-09-04T00:00:00Z"
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is True


# --- Task 15 Task 11 corrective v7, Section 25: an offset-MINUTE value
# outside 00-59 must be rejected -- Python's own datetime.fromisoformat()
# silently normalizes "+00:99" into a different (but validly-formed)
# offset ("+01:39") rather than raising, so semantic parsing ALONE is
# insufficient; an explicit range check on the parsed components is
# also required.


def test_rejects_candidate_with_timezone_offset_minute_out_of_range():
    run = dict(_make_candidate().run)
    run["started_at"] = "2026-01-01T00:00:00+00:99"
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


def test_rejects_candidate_with_timezone_offset_hour_out_of_range():
    run = dict(_make_candidate().run)
    run["started_at"] = "2026-01-01T00:00:00+99:00"
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


# --- Task 15 Task 11 corrective v7, Section 23/24: canonical string
# fields use `type(value) is str`, never `isinstance`, so a str
# SUBCLASS (which could carry hidden/overridden behavior while still
# comparing equal) is rejected even where it would otherwise satisfy a
# permissive isinstance-based check.


class _SneakyStr(str):
    """A str subclass -- `isinstance(_SneakyStr(...), str)` is True, but
    `type(_SneakyStr(...)) is str` is False."""


def test_rejects_candidate_with_str_subclass_package_sha256():
    proofs = dict(_GOOD_PROOFS)
    proofs["package_sha256"] = _SneakyStr("a" * 64)
    decision = validate_for_pass(_make_candidate(proofs=proofs))
    assert decision.eligible is False


def test_rejects_candidate_with_str_subclass_git_sha():
    run = dict(_make_candidate().run)
    run["git_sha"] = _SneakyStr("a" * 40)
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False


def test_rejects_candidate_with_str_subclass_run_id():
    run = dict(_make_candidate().run)
    run["run_id"] = _SneakyStr("rgoodrun00001")
    decision = validate_for_pass(_make_candidate(run=run))
    assert decision.eligible is False
