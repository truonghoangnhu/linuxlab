"""Task 15 Task 11 corrective v14 (design spec Sections 5, 9): closes
the independent v13 review's Critical/Important terminal-authority
findings.

1. `GateRunResult` used to validate only per-field EXACT TYPES, never
   the RELATIONAL coherence between `overall`/`exit_code`/`stage`/
   `evidence`/publication paths -- a `GateRunResult(overall="PASS",
   evidence=None, ...)` (or one wrapping failure evidence, or one whose
   `exit_code` disagreed with `evidence.exit_code`, or one carrying a
   partial publication-path set) all constructed successfully.
2. `evidence.validate_for_pass()`'s own `event_uuid` check coerced via
   `UUID(str(value))`, silently accepting a raw `uuid.UUID` object, an
   int, or bytes rather than requiring the canonical string it claims
   to validate.
3. `SignatureProof.verified` and `Authority`'s three fields used
   permissive `isinstance()`/no validation at all rather than exact
   canonical-type sealing.

These are the mandatory RED cases from the v14 design spec, Section 9.
"""

import uuid

import pytest

from scripts.release_gate.environment import Authority, PreflightError
from scripts.release_gate.evidence import (
    REQUIRED_SCENARIO_ASSERTIONS,
    REQUIRED_SCENARIOS,
    SCHEMA_VERSION,
    InvalidProofError,
    ReleaseEvidence,
    SignatureProof,
    UnsupportedEvidenceValueError,
    validate_for_pass,
)
from scripts.release_gate.gate_runner import GateRunResult, Stage


class _EvilStr(str):
    pass


class _BoolSpoof:
    """Not a real bool -- overrides `__class__` so `isinstance(x, bool)`
    is fooled, but `type(x) is bool` is not."""

    @property
    def __class__(self):
        return bool


# --- shared PASS-eligible evidence fixture -----------------------------


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


def _valid_run(run_id="rv14termauth1") -> dict:
    return {
        "run_id": run_id,
        "git_sha": "a" * 40,
        "started_at": "2026-09-10T00:00:00+00:00",
        "finished_at": "2026-09-10T00:05:00+00:00",
        "authority": {"authoritative": True, "platform": "linux", "reason": None},
    }


def pass_eligible_evidence(*, event_uuid=_VALID_EVENT_UUID, **overrides) -> ReleaseEvidence:
    proofs = {**_PASS_WORTHY_PROOFS, "outbox_to_inbox": {**_PASS_WORTHY_PROOFS["outbox_to_inbox"], "event_uuid": event_uuid}}
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
        "proofs": proofs,
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


def scenario_failure_evidence() -> ReleaseEvidence:
    return pass_eligible_evidence(
        overall="SCENARIO_FAILURE",
        exit_code=4,
        primary_failure={
            "category": "SCENARIO_FAILURE",
            "stage": "happy_path",
            "code": "AssertionError",
            "safe_message": "an internal failure occurred",
        },
    )


def valid_pass_result_kwargs() -> dict:
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


def valid_failure_result_kwargs() -> dict:
    return {
        "overall": "SCENARIO_FAILURE",
        "exit_code": 4,
        "evidence": None,
        "evidence_dir": None,
        "json_path": None,
        "markdown_path": None,
        "terminal_failure": None,
        "stage": Stage.DONE,
    }


# --- Section 5.1: PASS-coherence RED tests -----------------------------


def test_gate_result_accepts_a_genuinely_coherent_published_pass():
    result = GateRunResult(**valid_pass_result_kwargs())
    assert result.overall == "PASS"
    assert result.exit_code == 0


def test_pass_result_rejects_missing_evidence():
    kwargs = valid_pass_result_kwargs()
    kwargs["evidence"] = None
    kwargs["evidence_dir"] = None
    kwargs["json_path"] = None
    kwargs["markdown_path"] = None
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_pass_result_rejects_failure_evidence():
    kwargs = valid_pass_result_kwargs()
    kwargs["evidence"] = scenario_failure_evidence()
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_result_rejects_evidence_overall_disagreement():
    kwargs = valid_failure_result_kwargs()
    kwargs["overall"] = "STARTUP_FAILURE"
    kwargs["exit_code"] = 3
    kwargs["evidence"] = scenario_failure_evidence()
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_result_rejects_evidence_exit_code_disagreement():
    kwargs = valid_failure_result_kwargs()
    kwargs["overall"] = "SCENARIO_FAILURE"
    kwargs["exit_code"] = 3
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_result_rejects_overall_not_matching_the_exit_contract():
    kwargs = valid_failure_result_kwargs()
    kwargs["overall"] = "SCENARIO_FAILURE"
    kwargs["exit_code"] = 0
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_pass_result_rejects_retained_execution_resources():
    kwargs = valid_pass_result_kwargs()
    kwargs["evidence"] = pass_eligible_evidence(
        retention={
            "keep_on_failure_requested": False,
            "execution_resources_retained": True,
            "sanitized_debug_bundle_retained": True,
        }
    )
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_pass_result_rejects_non_done_stage():
    kwargs = valid_pass_result_kwargs()
    kwargs["stage"] = Stage.FINALIZE
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_pass_result_rejects_terminal_failure_present():
    kwargs = valid_pass_result_kwargs()
    kwargs["terminal_failure"] = {"category": "CLEANUP_FAILURE", "safe_message": "x"}
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


# --- Section 5.2: publication-path RED tests ----------------------------


@pytest.mark.parametrize("missing_field", ["evidence_dir", "json_path", "markdown_path"])
def test_published_result_rejects_partial_path_set(missing_field):
    kwargs = valid_pass_result_kwargs()
    kwargs[missing_field] = None
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_result_accepts_known_evidence_dir_with_failed_publication():
    """`evidence_dir` (the intended/reserved destination) is legitimately
    known independent of whether publication itself ever succeeds --
    `json_path`/`markdown_path` are populated ONLY by a genuinely
    successful publish, so a real publish-failure shape (destination
    known, nothing actually written) is valid, not "partial"."""
    kwargs = valid_failure_result_kwargs()
    kwargs["evidence"] = scenario_failure_evidence()
    kwargs["evidence_dir"] = "/tmp/evidence-dir"
    result = GateRunResult(**kwargs)
    assert result.evidence_dir == "/tmp/evidence-dir"
    assert result.json_path is None
    assert result.markdown_path is None


def test_result_rejects_paths_without_evidence():
    kwargs = valid_failure_result_kwargs()
    kwargs.update(
        evidence=None,
        evidence_dir="/tmp/evidence",
        json_path="/tmp/evidence/release-evidence.json",
        markdown_path="/tmp/evidence/release-evidence.md",
    )
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_pass_result_rejects_json_path_outside_evidence_dir():
    kwargs = valid_pass_result_kwargs()
    kwargs["json_path"] = "/tmp/other/release-evidence.json"
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_pass_result_rejects_markdown_path_outside_evidence_dir():
    kwargs = valid_pass_result_kwargs()
    kwargs["markdown_path"] = "/tmp/other/release-evidence.md"
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_pass_result_rejects_unexpected_json_filename():
    kwargs = valid_pass_result_kwargs()
    kwargs["json_path"] = "/tmp/evidence-dir/not-the-right-name.json"
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_pass_result_rejects_unexpected_markdown_filename():
    kwargs = valid_pass_result_kwargs()
    kwargs["markdown_path"] = "/tmp/evidence-dir/not-the-right-name.md"
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


# --- Section 5.3: final PASS validator UUID RED tests -------------------
#
# `ReleaseEvidence.__post_init__()` (via deep_freeze()) already rejects a
# non-canonical event_uuid at CONSTRUCTION time -- a raw UUID, bytes, an
# int, or a str subclass can never reach `pass_eligible_evidence()`'s own
# constructor call successfully. `validate_for_pass()` must still defend
# independently against a candidate that reached this state some OTHER
# way (its own docstring: "a candidate built by some other path... a
# hand-built object... must still be caught here"), so these probes use
# the lowest existing supported seam -- a normally-constructed, fully
# valid ReleaseEvidence with `.proofs` substituted afterward via
# `object.__setattr__` (the same mechanism ReleaseEvidence's own
# `__post_init__` already uses on itself) -- rather than loosening
# constructor sealing just to reach the validator.


def _evidence_with_tampered_event_uuid(bad_uuid) -> ReleaseEvidence:
    evidence = pass_eligible_evidence()
    tampered_outbox = dict(evidence.proofs["outbox_to_inbox"])
    tampered_outbox["event_uuid"] = bad_uuid
    tampered_proofs = dict(evidence.proofs)
    tampered_proofs["outbox_to_inbox"] = tampered_outbox
    object.__setattr__(evidence, "proofs", tampered_proofs)
    return evidence


@pytest.mark.parametrize("bad_uuid", [111111111111111111, b"bad-bytes-uuid"])
def test_validate_for_pass_rejects_non_string_event_uuid(bad_uuid):
    evidence = _evidence_with_tampered_event_uuid(bad_uuid)
    decision = validate_for_pass(evidence)
    assert decision.eligible is False
    assert "proof_outbox_event_uuid_invalid" in decision.reasons


def test_validate_for_pass_rejects_raw_uuid_object():
    evidence = _evidence_with_tampered_event_uuid(uuid.uuid4())
    decision = validate_for_pass(evidence)
    assert decision.eligible is False
    assert "proof_outbox_event_uuid_invalid" in decision.reasons


def test_validate_for_pass_rejects_string_subclass_event_uuid():
    evidence = _evidence_with_tampered_event_uuid(_EvilStr(_VALID_EVENT_UUID))
    decision = validate_for_pass(evidence)
    assert decision.eligible is False
    assert "proof_outbox_event_uuid_invalid" in decision.reasons


def test_validate_for_pass_accepts_canonical_event_uuid():
    evidence = pass_eligible_evidence()
    decision = validate_for_pass(evidence)
    assert decision.eligible is True


# --- Section 5.4: SignatureProof sealing --------------------------------


def test_signature_proof_accepts_exact_bool():
    proof = SignatureProof(verified=True)
    assert proof.verified is True


def test_signature_proof_rejects_bool_spoof():
    with pytest.raises(InvalidProofError):
        SignatureProof(verified=_BoolSpoof())


def test_signature_proof_rejects_int_for_verified():
    with pytest.raises(InvalidProofError):
        SignatureProof(verified=1)


# --- Section 5.4: Authority sealing -------------------------------------


def test_authority_accepts_valid_fields():
    authority = Authority(authoritative=False, platform="darwin", reason="development_only_platform")
    assert authority.platform == "darwin"


def test_authority_accepts_none_reason():
    authority = Authority(authoritative=True, platform="linux", reason=None)
    assert authority.reason is None


def test_authority_rejects_non_exact_authoritative_value():
    with pytest.raises(PreflightError):
        Authority(authoritative=1, platform="darwin", reason="development_only_platform")


def test_authority_rejects_bool_spoof_authoritative():
    with pytest.raises(PreflightError):
        Authority(authoritative=_BoolSpoof(), platform="darwin", reason="development_only_platform")


def test_authority_rejects_string_subclass_platform():
    with pytest.raises(PreflightError):
        Authority(authoritative=False, platform=_EvilStr("darwin"), reason="development_only_platform")


def test_authority_rejects_non_exact_reason_type():
    with pytest.raises(PreflightError):
        Authority(authoritative=False, platform="darwin", reason=_EvilStr("development_only_platform"))


def test_authority_rejects_bytes_platform():
    with pytest.raises(PreflightError):
        Authority(authoritative=False, platform=b"darwin", reason="development_only_platform")
