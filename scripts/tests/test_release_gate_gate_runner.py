"""Task 15 Task 11: release-gate orchestration -- exit-code mapping,
failure precedence, and evidence assembly/sanitization, tested with
fast fakes (a fake `dual_deployment_factory` and fake scenario
functions) so this logic is proven independently of real
Postgres/Gunicorn cycles. Real-infra proof that the DEFAULT scenario
list and the real dual_deployment() wire together correctly lives in
the CLI-level tests (scripts/tests/test_phase1_release_gate_cli.py),
which run the actual `scripts/phase1_release_gate.py` process.
"""

import inspect
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from scripts.release_gate import gate_runner
from scripts.release_gate.cleanup import CleanupAggregateError
from scripts.release_gate.environment import GateEnvironment, PreflightError
from scripts.release_gate.processes import StartupError

SENTINEL_NODE_SECRET = "sentinel-node-secret-9f3d2c1a"
SENTINEL_PRIVATE_KEY_PEM = "-----BEGIN SENTINEL PRIVATE KEY-----\nsentinel-pem-body\n-----END SENTINEL PRIVATE KEY-----\n"


@dataclass
class _FakeDeployment:
    node_secret: str = SENTINEL_NODE_SECRET
    private_key_path: object = None


@contextmanager
def _fake_deployment_cm(tmp_dir, raise_on_enter, raise_on_exit, deployment):
    if raise_on_enter is not None:
        raise raise_on_enter
    try:
        yield deployment if deployment is not None else _FakeDeployment()
    finally:
        if raise_on_exit is not None:
            raise raise_on_exit


def _make_factory(raise_on_enter=None, raise_on_exit=None, deployment=None):
    def factory(tmp_dir, **_ignored_kwargs):
        return _fake_deployment_cm(tmp_dir, raise_on_enter, raise_on_exit, deployment)

    return factory


def _fake_environment_builder(*, run_id="rfaketest0001", git_sha="deadbeefcafefeed", artifact_dir=None):
    def builder(*, repo_root, keep_on_failure=False):
        from scripts.release_gate.environment import DatabaseAdminConnection

        return GateEnvironment(
            run_id=run_id,
            git_sha=git_sha,
            repo_root=Path(repo_root),
            artifact_dir=artifact_dir if artifact_dir is not None else Path(repo_root) / "artifacts" / "phase1-release" / run_id,
            db_admin=DatabaseAdminConnection(host="127.0.0.1", port="5432", user="linuxlab", password="fake-admin-pw"),
            keep_on_failure=keep_on_failure,
        )

    return builder


def _always_pass(detail=None):
    def scenario(deployment):
        return detail or {}

    return scenario


def _always_fail(message="simulated scenario failure"):
    def scenario(deployment):
        raise AssertionError(message)

    return scenario


# --- exit codes ---------------------------------------------------------


def test_happy_pass_is_exit_0(tmp_path):
    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=_make_factory(),
        scenario_specs=(("fake_ok", _always_pass()),),
        tmp_root=tmp_path,
    )
    assert result.overall == "PASS"
    assert result.exit_code == 0


def test_keep_on_failure_flag_is_threaded_through_to_the_deployment_factory(tmp_path):
    observed = []

    def recording_factory(tmp_dir, **kwargs):
        observed.append(kwargs.get("keep_on_failure"))
        return _fake_deployment_cm(tmp_dir, None, None, None)

    gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        keep_on_failure=True,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=recording_factory,
        scenario_specs=(("fake_ok", _always_pass()),),
        tmp_root=tmp_path,
    )
    assert observed == [True]


def test_preflight_refusal_is_exit_2(tmp_path):
    def _failing_builder(*, repo_root, keep_on_failure=False):
        raise PreflightError("simulated preflight refusal")

    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_failing_builder,
        dual_deployment_factory=_make_factory(),
        scenario_specs=(("fake_ok", _always_pass()),),
        tmp_root=tmp_path,
    )
    assert result.overall == "PRECHECK_FAILURE"
    assert result.exit_code == 2
    assert result.evidence_dir is None, "precheck failure must not attempt to create an evidence directory"


def test_scenario_body_failure_is_exit_4(tmp_path):
    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=_make_factory(),
        scenario_specs=(("fake_fail", _always_fail()),),
        tmp_root=tmp_path,
    )
    assert result.overall == "SCENARIO_FAILURE"
    assert result.exit_code == 4


def test_startup_failure_before_any_scenario_body_runs_is_exit_3(tmp_path):
    entered_body = []

    def scenario(deployment):
        entered_body.append(True)
        return {}

    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=_make_factory(raise_on_enter=StartupError("simulated Gunicorn readiness failure")),
        scenario_specs=(("fake_startup_fail", scenario),),
        tmp_root=tmp_path,
    )
    assert result.overall == "STARTUP_FAILURE"
    assert result.exit_code == 3
    assert entered_body == [], "scenario body must never run when setup itself fails"


def test_cleanup_only_failure_is_exit_5(tmp_path):
    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=_make_factory(
            raise_on_exit=CleanupAggregateError([("drop_node_database", RuntimeError("simulated cleanup failure"))])
        ),
        scenario_specs=(("fake_ok", _always_pass()),),
        tmp_root=tmp_path,
    )
    assert result.overall == "CLEANUP_FAILURE"
    assert result.exit_code == 5
    assert result.evidence.cleanup_failure is not None
    assert "simulated cleanup failure" in result.evidence.cleanup_failure


# --- failure precedence --------------------------------------------------


def test_body_failure_and_cleanup_failure_both_occurring_scenario_wins_precedence(tmp_path):
    """Approved precedence: the scenario body's own classification
    remains primary; the cleanup failure is still surfaced, never
    dropped, just not what determines the exit code."""
    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=_make_factory(
            raise_on_exit=CleanupAggregateError([("drop_node_database", RuntimeError("simulated cleanup failure"))])
        ),
        scenario_specs=(("fake_fail", _always_fail("simulated body failure")),),
        tmp_root=tmp_path,
    )
    assert result.overall == "SCENARIO_FAILURE"
    assert result.exit_code == 4
    # cleanup failure must not disappear even though it lost precedence
    assert result.evidence.cleanup_failure is not None
    assert "simulated cleanup failure" in result.evidence.cleanup_failure
    scenario_detail = result.evidence.scenarios["fake_fail"]["detail"]
    assert "simulated body failure" in scenario_detail


def test_startup_failure_outranks_scenario_failure_across_the_run(tmp_path):
    startup_factory = _make_factory(raise_on_enter=StartupError("simulated startup failure"))
    ok_factory = _make_factory()

    call_order = {"n": 0}

    def dual_deployment_factory(tmp_dir, **kwargs):
        call_order["n"] += 1
        if call_order["n"] == 1:
            return ok_factory(tmp_dir, **kwargs)
        return startup_factory(tmp_dir, **kwargs)

    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=dual_deployment_factory,
        scenario_specs=(
            ("first_scenario_fails_body", _always_fail("simulated body failure")),
            ("second_scenario_fails_startup", _always_pass()),
        ),
        tmp_root=tmp_path,
    )
    assert result.overall == "STARTUP_FAILURE"
    assert result.exit_code == 3
    assert result.evidence.scenarios["first_scenario_fails_body"]["status"] == "FAIL"
    assert result.evidence.scenarios["second_scenario_fails_startup"]["status"] == "FAIL"


def test_all_scenarios_in_spec_are_attempted_and_recorded(tmp_path):
    """Even after an earlier scenario fails, later scenarios still run
    -- each gets its own fresh dual_deployment(), so failures never
    cascade -- and every one is recorded in evidence."""
    specs = (
        ("scenario_one", _always_fail("first failure")),
        ("scenario_two", _always_pass({"ok": True})),
        ("scenario_three", _always_pass({"ok": True})),
    )
    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=_make_factory(),
        scenario_specs=specs,
        tmp_root=tmp_path,
    )
    assert set(result.evidence.scenarios.keys()) == {"scenario_one", "scenario_two", "scenario_three"}
    assert result.evidence.scenarios["scenario_one"]["status"] == "FAIL"
    assert result.evidence.scenarios["scenario_two"]["status"] == "PASS"
    assert result.evidence.scenarios["scenario_three"]["status"] == "PASS"


# --- evidence contents ----------------------------------------------------


def test_evidence_json_has_schema_version_1_and_required_fields(tmp_path):
    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(run_id="rschematest01", git_sha="cafef00dbeefdead"),
        dual_deployment_factory=_make_factory(),
        scenario_specs=(("fake_ok", _always_pass({"event_id": "abc-123"})),),
        tmp_root=tmp_path,
    )
    doc = json.loads(result.json_path.read_text())
    assert doc["schema_version"] == 1
    assert doc["run_id"] == "rschematest01"
    assert doc["git_sha"] == "cafef00dbeefdead"
    assert doc.get("started_at")
    assert doc.get("finished_at")
    assert doc["overall"] == "PASS"
    assert "fake_ok" in doc["scenarios"]
    assert doc["artifacts"]["fake_ok"]["event_id"] == "abc-123"


def test_evidence_markdown_is_generated(tmp_path):
    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=_make_factory(),
        scenario_specs=(("fake_ok", _always_pass()),),
        tmp_root=tmp_path,
    )
    assert result.markdown_path.exists()
    text = result.markdown_path.read_text()
    assert "fake_ok" in text
    assert "PASS" in text


def test_evidence_dir_that_already_exists_is_a_precheck_failure(tmp_path):
    builder = _fake_environment_builder(run_id="rcollisiontest")
    env_probe = builder(repo_root=tmp_path)
    env_probe.artifact_dir.mkdir(parents=True)

    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=builder,
        dual_deployment_factory=_make_factory(),
        scenario_specs=(("fake_ok", _always_pass()),),
        tmp_root=tmp_path,
    )
    assert result.overall == "PRECHECK_FAILURE"
    assert result.exit_code == 2


# --- sanitization ----------------------------------------------------------


def test_node_secret_in_exception_text_is_redacted_from_evidence(tmp_path):
    def leaky_scenario(deployment):
        raise AssertionError(f"unexpected response containing {deployment.node_secret}")

    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=_make_factory(deployment=_FakeDeployment(node_secret=SENTINEL_NODE_SECRET)),
        scenario_specs=(("leaky", leaky_scenario),),
        tmp_root=tmp_path,
    )
    assert result.overall == "SCENARIO_FAILURE"
    json_text = result.json_path.read_text()
    markdown_text = result.markdown_path.read_text()
    assert SENTINEL_NODE_SECRET not in json_text
    assert SENTINEL_NODE_SECRET not in markdown_text


def test_private_key_pem_content_in_exception_text_is_redacted_from_evidence(tmp_path):
    key_path = tmp_path / "sentinel-signing.pem"
    key_path.write_text(SENTINEL_PRIVATE_KEY_PEM)

    def leaky_scenario(deployment):
        raise AssertionError(f"unexpected key material: {Path(deployment.private_key_path).read_text()}")

    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=_fake_environment_builder(),
        dual_deployment_factory=_make_factory(
            deployment=_FakeDeployment(node_secret=SENTINEL_NODE_SECRET, private_key_path=key_path)
        ),
        scenario_specs=(("leaky", leaky_scenario),),
        tmp_root=tmp_path,
    )
    assert result.overall == "SCENARIO_FAILURE"
    json_text = result.json_path.read_text()
    markdown_text = result.markdown_path.read_text()
    assert SENTINEL_PRIVATE_KEY_PEM not in json_text
    assert "sentinel-pem-body" not in json_text
    assert SENTINEL_PRIVATE_KEY_PEM not in markdown_text
    assert "sentinel-pem-body" not in markdown_text


def test_db_admin_password_in_exception_text_is_redacted_from_evidence(tmp_path):
    """The DB admin password is registered as a known secret the
    instant build_gate_environment() returns -- before any scenario
    runs -- so even a scenario failure whose message happens to embed
    it must still come out redacted."""
    sentinel_password = "sentinel-db-admin-password-77abf2"

    def builder(*, repo_root, keep_on_failure=False):
        from scripts.release_gate.environment import DatabaseAdminConnection

        return GateEnvironment(
            run_id="rdbpwtest0001",
            git_sha="0" * 40,
            repo_root=Path(repo_root),
            artifact_dir=Path(repo_root) / "artifacts" / "phase1-release" / "rdbpwtest0001",
            db_admin=DatabaseAdminConnection(
                host="127.0.0.1", port="5432", user="linuxlab", password=sentinel_password
            ),
            keep_on_failure=keep_on_failure,
        )

    def leaky_scenario(deployment):
        raise AssertionError(f"unexpected admin password in scope: {sentinel_password}")

    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        environment_builder=builder,
        dual_deployment_factory=_make_factory(),
        scenario_specs=(("leaky", leaky_scenario),),
        tmp_root=tmp_path,
    )
    assert result.overall == "SCENARIO_FAILURE"
    json_text = result.json_path.read_text()
    markdown_text = result.markdown_path.read_text()
    assert sentinel_password not in json_text
    assert sentinel_password not in markdown_text


# --- no auto-tag / auto-commit / auto-push --------------------------------


def test_gate_runner_never_shells_out_to_git_tag_commit_or_push():
    """Structural proof: this module orchestrates the release gate but
    must never create a git tag, commit, or push -- that remains a
    human-controlled action outside Task 11's scope."""
    source = inspect.getsource(gate_runner)
    for forbidden in ("git tag", "git commit", "git push", '"tag"', "'tag'"):
        assert forbidden not in source
