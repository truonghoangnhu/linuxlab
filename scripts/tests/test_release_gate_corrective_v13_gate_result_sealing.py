"""Task 15 Task 11 corrective v13 (design spec Sections 4-6, Part C
31-32): `GateRunResult.evidence` is a closed union (exact
`ReleaseEvidence` or `None` -- never a dict, mapping proxy, or
`ReleaseEvidence` subclass), and every terminal path field
(`evidence_dir`, `json_path`, `markdown_path`) holds a canonical `str`
or `None` -- never a retained `pathlib.Path`/`PathLike` identity.

Before this corrective, `GateRunResult.__post_init__` validated
`overall`/`exit_code`/`stage` and required `evidence_dir`/`json_path`/
`markdown_path` to be an exact `pathlib.Path` instance (corrective
v12's own `test_gate_run_result_rejects_str_path_for_evidence_dir`),
but never examined `evidence` at all -- a bare dict, a
`types.MappingProxyType`, or a hand-built `ReleaseEvidence` subclass
could all be assigned to a frozen `GateRunResult.evidence` field
completely unexamined. Corrective v13 both closes that gap AND flips
the terminal-path representation from `Path` to canonical `str`
(spec Section 6: "Terminal paths use canonical strings" -- no
`Path`/`PosixPath`/`WindowsPath`/`PathLike` identity may survive into
terminal result truth), which necessarily supersedes v12's own
Path-exactness test for these three fields.
"""

from pathlib import Path
from types import MappingProxyType

import pytest

from scripts.release_gate.evidence import ReleaseEvidence, UnsupportedEvidenceValueError
from scripts.release_gate.gate_runner import GateRunResult, Stage


class _EvilPath(type(Path("/tmp/x"))):
    """A `Path` subclass -- satisfies `isinstance(value, Path)` but is
    not the canonical `str` this corrective now requires."""


class _OpaquePathLike:
    """An arbitrary `os.PathLike` -- must never be trusted to convert
    itself inside the terminal constructor."""

    def __fspath__(self):
        return "/tmp/opaque-pathlike"


def _valid_run_kwargs() -> dict:
    return {
        "run_id": "rgateresultsealing1",
        "git_sha": "a" * 40,
        "started_at": "2026-09-07T00:00:00+00:00",
        "finished_at": "2026-09-07T00:05:00+00:00",
        "authority": {"authoritative": True, "platform": "linux", "reason": None},
    }


def _base_evidence_kwargs() -> dict:
    return {
        "schema_version": 1,
        "run": _valid_run_kwargs(),
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
    }


def valid_evidence() -> ReleaseEvidence:
    """Task 15 Task 11 corrective v14, Section 5.1: a non-PASS-shaped
    evidence instance -- `GateRunResult`'s own coherence gate now
    requires `evidence.overall`/`evidence.exit_code` to agree with the
    RESULT's `overall`/`exit_code` whenever evidence is present at all
    (not only for PASS), so this file's evidence-IDENTITY tests (is it
    an exact ReleaseEvidence, never a dict/proxy/subclass?) use a
    result shape that never has to satisfy the full PASS-coherence
    matrix -- see `pass_eligible_evidence()` below for the one that
    does."""
    kwargs = _base_evidence_kwargs()
    kwargs.update(overall="CLEANUP_FAILURE", exit_code=5, cleanup_status="failed")
    return ReleaseEvidence(**kwargs)


def pass_eligible_evidence() -> ReleaseEvidence:
    """Genuinely `validate_for_pass()`-eligible -- required for any test
    exercising `GateRunResult`'s PASS-coherence matrix (spec Section
    5.1), as opposed to `valid_evidence()` above, which deliberately is
    not PASS-shaped."""
    from scripts.release_gate.evidence import REQUIRED_SCENARIO_ASSERTIONS, REQUIRED_SCENARIOS

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

    kwargs = _base_evidence_kwargs()
    kwargs.update(
        overall="PASS",
        exit_code=0,
        scenarios={
            name: {
                "name": name,
                "status": "PASS",
                "assertions": _pass_worthy_assertions_for(name),
                "duration_ms": 1.0,
                "detail": "",
            }
            for name in REQUIRED_SCENARIOS
        },
        proofs={
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
        },
    )
    return ReleaseEvidence(**kwargs)


def valid_gate_result_kwargs() -> dict:
    """A minimal, coherent NON-PASS shape with no evidence and no
    publication paths -- the base every evidence-IDENTITY and terminal-
    path-TYPE test below starts from and overrides exactly one field
    of, without also having to satisfy the PASS-coherence matrix."""
    return {
        "overall": "CLEANUP_FAILURE",
        "exit_code": 5,
        "evidence": None,
        "evidence_dir": None,
        "json_path": None,
        "markdown_path": None,
        "terminal_failure": None,
        "stage": Stage.DONE,
    }


def valid_pass_result_kwargs() -> dict:
    """A genuinely coherent, published PASS -- satisfies every
    simultaneous condition in the v14 PASS-coherence matrix."""
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


# --- Section 4/31: GateRunResult.evidence is a closed union -----------------


def test_gate_result_accepts_none_evidence():
    result = GateRunResult(**valid_gate_result_kwargs())
    assert result.evidence is None


def test_gate_result_accepts_exact_release_evidence():
    evidence = valid_evidence()
    kwargs = valid_gate_result_kwargs()
    kwargs["evidence"] = evidence
    result = GateRunResult(**kwargs)
    assert result.evidence is evidence


def test_gate_result_rejects_dict_evidence():
    kwargs = valid_gate_result_kwargs()
    kwargs["evidence"] = {}
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_gate_result_rejects_mapping_proxy_evidence():
    kwargs = valid_gate_result_kwargs()
    kwargs["evidence"] = MappingProxyType({})
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_gate_result_rejects_opaque_object_evidence():
    kwargs = valid_gate_result_kwargs()
    kwargs["evidence"] = object()
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_gate_result_rejects_release_evidence_subclass():
    class EvilEvidence(ReleaseEvidence):
        pass

    evidence = valid_evidence()
    subclass_instance = EvilEvidence(**evidence.to_dict())
    kwargs = valid_gate_result_kwargs()
    kwargs["evidence"] = subclass_instance
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


# --- Section 6/32: terminal paths are canonical strings ----------------------


def test_gate_result_accepts_canonical_path_strings():
    result = GateRunResult(**valid_pass_result_kwargs())
    assert type(result.evidence_dir) is str
    assert type(result.json_path) is str
    assert type(result.markdown_path) is str


@pytest.mark.parametrize("field_name", ["evidence_dir", "json_path", "markdown_path"])
def test_gate_result_rejects_path_object_for_terminal_path_fields(field_name):
    """Supersedes corrective v12's `test_gate_run_result_rejects_str_
    path_for_evidence_dir` -- v13 flips the required representation
    from Path to canonical str (spec Section 6/49: "Path issue solved
    without impossible exact Path type requirement")."""
    kwargs = valid_gate_result_kwargs()
    kwargs[field_name] = Path("/tmp/evidence-dir")
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


@pytest.mark.parametrize("field_name", ["evidence_dir", "json_path", "markdown_path"])
def test_gate_result_rejects_path_subclass_for_terminal_path_fields(field_name):
    kwargs = valid_gate_result_kwargs()
    kwargs[field_name] = _EvilPath("/tmp/evidence-dir")
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


@pytest.mark.parametrize("field_name", ["evidence_dir", "json_path", "markdown_path"])
def test_gate_result_rejects_opaque_pathlike_for_terminal_path_fields(field_name):
    kwargs = valid_gate_result_kwargs()
    kwargs[field_name] = _OpaquePathLike()
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


@pytest.mark.parametrize("field_name", ["evidence_dir", "json_path", "markdown_path"])
def test_gate_result_rejects_bytes_path_for_terminal_path_fields(field_name):
    kwargs = valid_gate_result_kwargs()
    kwargs[field_name] = b"/tmp/evidence-dir"
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


# --- Section 5: result-shape consistency (existing valid shapes unchanged) --


def test_gate_result_pass_shape_with_evidence_and_paths_is_valid():
    result = GateRunResult(**valid_pass_result_kwargs())
    assert result.overall == "PASS"


def test_gate_result_emergency_shape_with_no_evidence_and_no_paths_is_valid():
    kwargs = valid_gate_result_kwargs()
    kwargs.update(
        overall="CLEANUP_FAILURE",
        exit_code=5,
        terminal_failure={
            "category": "CLEANUP_FAILURE",
            "stage": "cleanup",
            "code": "EmergencyTerminalFailure",
            "safe_message": "an internal release-gate failure could not be safely detailed",
        },
    )
    result = GateRunResult(**kwargs)
    assert result.evidence is None
    assert result.evidence_dir is None
