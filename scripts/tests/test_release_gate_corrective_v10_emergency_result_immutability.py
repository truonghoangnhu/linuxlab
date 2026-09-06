"""Task 15 Task 11 corrective v10, Gap 1 (design spec Sections 4-6):
`GateRunResult` was a plain (non-frozen) dataclass with a mutable
`terminal_failure: dict | None` field, and `_EMERGENCY_RESULTS` is a
module-level global dict built ONCE at import time whose cached
`GateRunResult` objects are returned BY REFERENCE on every call to
`_select_emergency_result()`. Before this corrective, a caller could
mutate a cached emergency result's `overall`/`exit_code` (or its nested
`terminal_failure` dict) and PERMANENTLY corrupt that cached global for
every subsequent run that hits the same emergency path for the rest of
the process's lifetime -- with no test catching it.

These are the mandatory RED tests from the v10 design spec, Section 25,
items 1-3.
"""

import dataclasses
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.release_gate import gate_runner
from scripts.release_gate.environment import DatabaseAdminConnection, GateEnvironment

# --- shared fast-fake plumbing (no real Postgres/Gunicorn needed here --
# --- only orchestration-level behavior is under test) ----------------------


def _fake_environment_builder(*, repo_root, keep_on_failure=False):
    run_id = "rpoisontest1"
    return GateEnvironment(
        run_id=run_id,
        git_sha="a" * 40,
        repo_root=Path(repo_root),
        artifact_dir=Path(repo_root) / "artifacts" / "phase1-release" / run_id,
        db_admin=DatabaseAdminConnection(host="127.0.0.1", port="5432", user="linuxlab", password=""),
        keep_on_failure=keep_on_failure,
    )


def _fake_run_validation(env):
    return {
        "clean_tree": True,
        "central_check": "ok",
        "node_check": "ok",
        "central_migration_drift": "none",
        "node_migration_drift": "none",
    }


@dataclass
class _FakeDeployment:
    node_secret: str = "sentinel"
    private_key_path: object = None
    tmp_dir: object = None
    resource_ledger: object = None


@contextmanager
def _fake_dual_deployment(tmp_dir, **kwargs):
    yield _FakeDeployment(tmp_dir=Path(tmp_dir))


def _always_fail_scenario(deployment):
    raise AssertionError("simulated scenario failure")


def _run_forcing_emergency_path(tmp_path, monkeypatch):
    """Forces the run all the way down to `_select_emergency_result()`:
    a real scenario failure sets `state.primary_failure` to
    SCENARIO_FAILURE, then `_build_snapshot()` is monkeypatched to raise
    PERSISTENTLY (on both the primary attempt AND the fallback attempt
    inside `_finalize()`), so the failure escapes `_finalize()` entirely
    and `_safe_finalize()`'s own outer boundary falls back to the
    prebuilt emergency result -- exactly the path spec Section 6
    requires this test to exercise for real, never merely calling
    `_select_emergency_result()` as a unit in isolation."""
    monkeypatch.setattr(gate_runner, "assert_linux_platform", lambda: None)
    monkeypatch.setattr(
        gate_runner,
        "_build_snapshot",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("simulated persistent finalize failure")),
    )
    return gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        scenario_specs=(("fake_fail", _always_fail_scenario),),
        dual_deployment_factory=_fake_dual_deployment,
        environment_builder=_fake_environment_builder,
        run_validation=_fake_run_validation,
        tmp_root=tmp_path,
    )


# --- RED test 25.1: ordinary (non-emergency) GateRunResult mutation ---------


def test_ordinary_gate_run_result_top_level_mutation_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_runner, "assert_linux_platform", lambda: None)

    def _failing_builder(*, repo_root, keep_on_failure=False):
        raise gate_runner.PreflightError("simulated preflight refusal")

    result = gate_runner.run_release_gate(
        repo_root=tmp_path, check_platform=False, environment_builder=_failing_builder
    )
    assert result.overall == "PRECHECK_FAILURE"

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.overall = "PASS"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.exit_code = 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.evidence = None
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.stage = "DONE"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.terminal_failure = {"category": "PASS"}


# --- RED test 25.2: cached emergency GateRunResult mutation -----------------


def test_cached_emergency_results_top_level_and_nested_mutation_is_rejected():
    for result in gate_runner._EMERGENCY_RESULTS.values():
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.overall = "PASS"
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.exit_code = 0
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.evidence = object()
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.stage = "MUTATED"
        # Nested mutation of the terminal_failure dict.
        assert result.terminal_failure is not None
        with pytest.raises(TypeError):
            result.terminal_failure["category"] = "PASS"
        with pytest.raises(TypeError):
            result.terminal_failure["safe_message"] = "mutated"


# --- RED test 25.3: cached emergency poison regression ----------------------


def test_cached_emergency_result_poison_regression_across_two_runs(tmp_path, monkeypatch):
    """Attempt every mutation on a real emergency result returned from a
    real failing run, then run a SECOND real failing run through the
    EXACT SAME emergency path and prove it is still the original,
    deterministic, unpoisoned failure -- no publication occurred either
    time."""
    result1 = _run_forcing_emergency_path(tmp_path, monkeypatch)
    assert result1.overall == "SCENARIO_FAILURE"
    assert result1.exit_code == 4
    assert result1.evidence is None
    assert result1.evidence_dir is None
    assert result1.json_path is None
    assert result1.markdown_path is None

    # Attempt every mutation on the returned (cached) emergency result.
    with pytest.raises(dataclasses.FrozenInstanceError):
        result1.overall = "PASS"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result1.exit_code = 0
    with pytest.raises(TypeError):
        result1.terminal_failure["category"] = "PASS"
    with pytest.raises(TypeError):
        result1.terminal_failure["safe_message"] = "mutated"

    # A second real failing run through the exact same emergency path.
    result2 = _run_forcing_emergency_path(tmp_path, monkeypatch)
    assert result2.overall == "SCENARIO_FAILURE"
    assert result2.exit_code == 4
    assert result2.evidence is None
    assert result2.json_path is None
    assert result2.markdown_path is None
    # It is provably the SAME cached global object, still uncorrupted --
    # the strongest possible proof that no poisoning occurred.
    assert result2 is result1
    assert result2.terminal_failure["category"] == "SCENARIO_FAILURE"
