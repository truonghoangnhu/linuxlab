"""Task 15 Task 11 corrective v12 (design spec Sections 9-13, 27): the
closed fail-closed freeze/export algebra, and construction-sealing for
`GateRunResult` independent of `ReleaseEvidence`.

Two blockers closed here:

1. `deep_freeze()` previously treated raw `bytes` as a known-safe leaf
   (Section 4/10: `bytes` is not part of the canonical terminal scalar
   algebra -- no real call site in this codebase ever actually needed
   it, per `_SAFE_LEAF_TYPES`'s own docstring).
2. `_thaw()` (the renderer-export counterpart) had an unconditional
   `return value` fallback for anything that was not a
   `MappingProxyType`/`tuple`/`frozenset` -- an unsupported value would
   be passed through unchanged rather than rejected, so export was not
   actually fail-closed over the same algebra `deep_freeze()` builds.

These are the mandatory RED tests from the v12 design spec, Section 29/
30, plus the `GateRunResult` field-matrix and emergency-cache poisoning
regressions from Section 28.
"""

from pathlib import Path

import pytest

from scripts.release_gate.evidence import (
    ReleaseEvidence,
    UnsupportedEvidenceValueError,
    _thaw,
    deep_freeze,
)
from scripts.release_gate.gate_runner import GateRunResult, Stage


class _EvilStr(str):
    pass


class _EvilInt(int):
    pass


# --- Section 29: bytes / unsupported values must fail closed ---------------


def test_deep_freeze_rejects_raw_bytes():
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze(b"raw-bytes")


def test_deep_freeze_rejects_bytearray():
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze(bytearray(b"raw-bytes"))


def test_deep_freeze_rejects_bytes_nested_in_mapping():
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze({"key": b"raw-bytes"})


def test_thaw_rejects_bytes():
    with pytest.raises(UnsupportedEvidenceValueError):
        _thaw(b"raw-bytes")


def test_thaw_rejects_unknown_custom_object():
    with pytest.raises(UnsupportedEvidenceValueError):
        _thaw(object())


def test_thaw_rejects_path():
    with pytest.raises(UnsupportedEvidenceValueError):
        _thaw(Path("/tmp/x"))


def test_thaw_still_accepts_exact_builtin_scalars():
    assert _thaw("x") == "x"
    assert _thaw(1) == 1
    assert _thaw(1.5) == 1.5
    assert _thaw(True) is True
    assert _thaw(None) is None


# --- Section 30: to_dict() walks to exact JSON-safe builtins only -----------


def _valid_release_evidence_kwargs() -> dict:
    return {
        "schema_version": 1,
        "run": {
            "run_id": "rexportalgebra1",
            "git_sha": "a" * 40,
            "started_at": "2026-09-07T00:00:00+00:00",
            "finished_at": "2026-09-07T00:05:00+00:00",
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
        "scenarios": {
            "happy_path": {
                "name": "happy_path",
                "status": "PASS",
                "assertions": {"package_installed_and_activated": True},
                "duration_ms": 12.5,
                "detail": "",
            }
        },
        "proofs": {"package_sha256": "a" * 64},
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


def _assert_exact_builtin_tree(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert type(key) is str
            _assert_exact_builtin_tree(item)
    elif isinstance(value, list):
        for item in value:
            _assert_exact_builtin_tree(item)
    else:
        assert type(value) in (str, int, float, bool, type(None))


def test_to_dict_walks_to_exact_builtin_leaves_only():
    evidence = ReleaseEvidence(**_valid_release_evidence_kwargs())
    doc = evidence.to_dict()
    _assert_exact_builtin_tree(doc)


def test_mutating_to_dict_export_never_mutates_the_frozen_snapshot():
    evidence = ReleaseEvidence(**_valid_release_evidence_kwargs())
    doc = evidence.to_dict()
    doc["scenarios"]["happy_path"]["assertions"]["package_installed_and_activated"] = False
    doc["validation"]["clean_tree"] = False
    assert evidence.scenarios["happy_path"]["assertions"]["package_installed_and_activated"] is True
    assert evidence.validation["clean_tree"] is True


# --- GateRunResult field-matrix sealing --------------------------------------


def _valid_gate_run_result_kwargs() -> dict:
    return {
        "overall": "PASS",
        "exit_code": 0,
        "evidence": None,
        "evidence_dir": None,
        "json_path": None,
        "markdown_path": None,
        "terminal_failure": None,
        "stage": Stage.DONE,
    }


def test_gate_run_result_accepts_exact_builtin_fields():
    result = GateRunResult(**_valid_gate_run_result_kwargs())
    assert result.overall == "PASS"


def test_gate_run_result_rejects_str_subclass_overall():
    kwargs = _valid_gate_run_result_kwargs()
    kwargs["overall"] = _EvilStr("PASS")
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_gate_run_result_rejects_int_subclass_exit_code():
    kwargs = _valid_gate_run_result_kwargs()
    kwargs["exit_code"] = _EvilInt(0)
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_gate_run_result_rejects_bool_exit_code():
    kwargs = _valid_gate_run_result_kwargs()
    kwargs["exit_code"] = True
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_gate_run_result_rejects_str_subclass_stage():
    kwargs = _valid_gate_run_result_kwargs()
    kwargs["stage"] = _EvilStr(Stage.DONE)
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_gate_run_result_rejects_path_object_for_evidence_dir():
    """Task 15 Task 11 corrective v13, Section 6/49: supersedes this
    file's own prior `test_gate_run_result_rejects_str_path_for_
    evidence_dir` -- terminal path fields are now canonical `str`, not
    `pathlib.Path` (no Path/PathLike identity may survive into terminal
    result truth), so a `Path` object is what must be rejected now, and
    a plain `str` is exactly what is required."""
    kwargs = _valid_gate_run_result_kwargs()
    kwargs["evidence_dir"] = Path("/tmp/not-a-str")
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_gate_run_result_rejects_terminal_failure_with_bytes_leaf():
    kwargs = _valid_gate_run_result_kwargs()
    kwargs["terminal_failure"] = {"category": "CLEANUP_FAILURE", "safe_message": b"evil"}
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


def test_gate_run_result_rejects_terminal_failure_with_str_subclass_leaf():
    kwargs = _valid_gate_run_result_kwargs()
    kwargs["terminal_failure"] = {"category": _EvilStr("CLEANUP_FAILURE")}
    with pytest.raises(UnsupportedEvidenceValueError):
        GateRunResult(**kwargs)


# --- Section 28: emergency-cache poisoning regression ------------------------


def test_emergency_result_graph_is_constructor_sealed_and_immutable():
    import dataclasses

    from scripts.release_gate import gate_runner

    for result in gate_runner._EMERGENCY_RESULTS.values():
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.overall = "PASS"
