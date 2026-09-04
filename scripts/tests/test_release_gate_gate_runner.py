"""Task 15 Task 11 (+ corrective): release-gate orchestration -- exit-
code mapping, failure precedence, evidence assembly/sanitization,
tested with fast fakes (a fake `dual_deployment_factory`, fake
scenario functions, and a fake `run_validation`) so this logic is
proven independently of real Postgres/Gunicorn cycles and real
`manage.py` subprocess calls. Real-infra proof that the DEFAULT
scenario list and the real dual_deployment()/validation checks wire
together correctly lives in the CLI-level tests
(scripts/tests/test_phase1_release_gate_cli.py) and the manual
end-to-end verification runs, which run the actual
`scripts/phase1_release_gate.py` process / real `run_release_gate()`.
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
SENTINEL_ANSWER_KEY = "sentinel-answer-key-material-77bc"
SENTINEL_STDIN_PAYLOAD = "sentinel-stdin-payload-44ab"

_OK_VALIDATION = {
    "clean_tree": True,
    "central_check": "ok",
    "node_check": "ok",
    "central_migration_drift": "none",
    "node_migration_drift": "none",
}


def _fake_run_validation(validation=None):
    result = dict(_OK_VALIDATION) if validation is None else dict(validation)

    def _run(env):
        return result

    return _run


@dataclass
class _FakeDeployment:
    node_secret: str = SENTINEL_NODE_SECRET
    private_key_path: object = None
    tmp_dir: object = None
    gate_names: object = None


class _FakeGateNames:
    def __init__(self, run_id):
        self.central = f"linuxlab_gate_central_{run_id}"
        self.node = f"linuxlab_gate_node_{run_id}"


@contextmanager
def _fake_deployment_cm(tmp_dir, run_id, raise_on_enter, raise_on_exit, deployment):
    # `raise_on_enter` is raised INSIDE the try (never before it) so
    # `finally` -- and therefore `raise_on_exit` -- still runs
    # regardless of when setup fails, exactly like the real
    # dual_deployment(): its own try/finally wraps setup AND the
    # yielded body alike, so cleanup is always attempted.
    try:
        if raise_on_enter is not None:
            raise raise_on_enter
        dep = deployment if deployment is not None else _FakeDeployment(tmp_dir=Path(tmp_dir), gate_names=_FakeGateNames(run_id))
        yield dep
    finally:
        if raise_on_exit is not None:
            raise raise_on_exit


def _make_factory(raise_on_enter=None, raise_on_exit=None, deployment=None):
    def factory(tmp_dir, **kwargs):
        return _fake_deployment_cm(tmp_dir, kwargs.get("run_id"), raise_on_enter, raise_on_exit, deployment)

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


def _run(**overrides):
    kwargs = {
        "repo_root": overrides.pop("repo_root"),
        "check_platform": False,
        "environment_builder": _fake_environment_builder(),
        "dual_deployment_factory": _make_factory(),
        "scenario_specs": (("fake_ok", _always_pass()),),
        "run_validation": _fake_run_validation(),
        "tmp_root": None,
    }
    kwargs.update(overrides)
    if kwargs["tmp_root"] is None:
        kwargs["tmp_root"] = kwargs["repo_root"]
    return gate_runner.run_release_gate(**kwargs)


# --- exit codes ---------------------------------------------------------


def test_happy_pass_is_exit_0(tmp_path):
    result = _run(repo_root=tmp_path)
    assert result.overall == "PASS"
    assert result.exit_code == 0
    assert result.evidence.validation == _OK_VALIDATION


def test_preflight_refusal_is_exit_2(tmp_path):
    def _failing_builder(*, repo_root, keep_on_failure=False):
        raise PreflightError("simulated preflight refusal")

    result = _run(repo_root=tmp_path, environment_builder=_failing_builder)
    assert result.overall == "PRECHECK_FAILURE"
    assert result.exit_code == 2
    assert result.evidence_dir is None, "precheck failure must not attempt to create an evidence directory"


def test_failed_validation_check_is_a_precheck_failure():
    """Design spec Section 12 / FAST CI parity: a PASS must never carry
    real validation failures silently -- Central/Node manage.py check
    or migration drift failing blocks the release the same way a dirty
    tree does."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = _run(
            repo_root=Path(tmp),
            run_validation=_fake_run_validation({**_OK_VALIDATION, "central_migration_drift": "detected"}),
        )
    assert result.overall == "PRECHECK_FAILURE"
    assert result.exit_code == 2


def test_scenario_body_failure_is_exit_4(tmp_path):
    result = _run(repo_root=tmp_path, scenario_specs=(("fake_fail", _always_fail()),))
    assert result.overall == "SCENARIO_FAILURE"
    assert result.exit_code == 4


def test_startup_failure_before_any_scenario_body_runs_is_exit_3(tmp_path):
    entered_body = []

    def scenario(deployment):
        entered_body.append(True)
        return {}

    result = _run(
        repo_root=tmp_path,
        dual_deployment_factory=_make_factory(raise_on_enter=StartupError("simulated Gunicorn readiness failure")),
        scenario_specs=(("fake_startup_fail", scenario),),
    )
    assert result.overall == "STARTUP_FAILURE"
    assert result.exit_code == 3
    assert entered_body == [], "scenario body must never run when setup itself fails"


def test_cleanup_only_failure_is_exit_5(tmp_path):
    result = _run(
        repo_root=tmp_path,
        dual_deployment_factory=_make_factory(
            raise_on_exit=CleanupAggregateError([("drop_node_database", RuntimeError("simulated cleanup failure"))])
        ),
    )
    assert result.overall == "CLEANUP_FAILURE"
    assert result.exit_code == 5
    assert result.evidence.cleanup_failure is not None
    assert result.evidence.cleanup_status == "failed"


# --- failure precedence --------------------------------------------------


def test_body_failure_and_cleanup_failure_both_occurring_scenario_wins_precedence(tmp_path):
    """Approved precedence: the scenario body's own classification
    remains primary; the cleanup failure is still surfaced, never
    dropped, just not what determines the exit code."""
    result = _run(
        repo_root=tmp_path,
        dual_deployment_factory=_make_factory(
            raise_on_exit=CleanupAggregateError([("drop_node_database", RuntimeError("simulated cleanup failure"))])
        ),
        scenario_specs=(("fake_fail", _always_fail("simulated body failure")),),
    )
    assert result.overall == "SCENARIO_FAILURE"
    assert result.exit_code == 4
    assert result.evidence.cleanup_failure is not None
    assert result.evidence.primary_failure["category"] == "SCENARIO_FAILURE"
    assert result.evidence.primary_failure["code"] == "AssertionError"


def test_startup_failure_and_cleanup_failure_both_occurring_startup_wins_precedence(tmp_path):
    """Task 11 corrective, Important finding #2: a startup failure
    followed by a cleanup failure must remain STARTUP_FAILURE (exit 3),
    never silently demoted to CLEANUP_FAILURE (exit 5) just because the
    scenario body never got a chance to run."""
    result = _run(
        repo_root=tmp_path,
        dual_deployment_factory=_make_factory(
            raise_on_enter=StartupError("simulated Gunicorn readiness failure"),
            raise_on_exit=CleanupAggregateError([("drop_node_database", RuntimeError("simulated cleanup failure"))]),
        ),
    )
    assert result.overall == "STARTUP_FAILURE"
    assert result.exit_code == 3
    assert result.evidence.cleanup_failure is not None, "cleanup failure must still be surfaced, never dropped"
    assert result.evidence.primary_failure["category"] == "STARTUP_FAILURE"
    assert result.evidence.primary_failure["code"] == "StartupError"


def test_startup_failure_outranks_scenario_failure_across_the_run(tmp_path):
    startup_factory = _make_factory(raise_on_enter=StartupError("simulated startup failure"))
    ok_factory = _make_factory()

    call_order = {"n": 0}

    def dual_deployment_factory(tmp_dir, **kwargs):
        call_order["n"] += 1
        if call_order["n"] == 1:
            return ok_factory(tmp_dir, **kwargs)
        return startup_factory(tmp_dir, **kwargs)

    result = _run(
        repo_root=tmp_path,
        dual_deployment_factory=dual_deployment_factory,
        scenario_specs=(
            ("first_scenario_fails_body", _always_fail("simulated body failure")),
            ("second_scenario_fails_startup", _always_pass()),
        ),
    )
    assert result.overall == "STARTUP_FAILURE"
    assert result.exit_code == 3
    assert result.evidence.scenarios["first_scenario_fails_body"]["status"] == "FAIL"
    assert result.evidence.scenarios["second_scenario_fails_startup"]["status"] == "FAIL"
    # The primary failure must describe the WORSE (startup) scenario,
    # never the earlier-but-less-severe body failure.
    assert result.evidence.primary_failure["stage"] == "second_scenario_fails_startup"
    assert result.evidence.primary_failure["category"] == "STARTUP_FAILURE"


def test_keep_on_failure_flag_is_threaded_through_to_the_deployment_factory(tmp_path):
    observed = []

    def recording_factory(tmp_dir, **kwargs):
        observed.append(kwargs.get("keep_on_failure"))
        return _fake_deployment_cm(tmp_dir, kwargs.get("run_id"), None, None, None)

    _run(repo_root=tmp_path, keep_on_failure=True, dual_deployment_factory=recording_factory)
    assert observed == [True]


def test_child_run_id_is_derived_from_the_outer_run_id(tmp_path):
    """Task 11 corrective Section 4: every scenario's own dual_deployment
    identity must be traceable back to the ONE outer run_id, never an
    unrelated random one."""
    observed_run_ids = []

    def recording_factory(tmp_dir, **kwargs):
        observed_run_ids.append(kwargs.get("run_id"))
        return _fake_deployment_cm(tmp_dir, kwargs.get("run_id"), None, None, None)

    _run(
        repo_root=tmp_path,
        environment_builder=_fake_environment_builder(run_id="router0000001"),
        dual_deployment_factory=recording_factory,
        scenario_specs=(("happy_path", _always_pass()),),
    )
    assert len(observed_run_ids) == 1
    assert observed_run_ids[0] is not None
    assert observed_run_ids[0].startswith("router0000001")


# --- all scenarios attempted / recorded -----------------------------------


def test_all_scenarios_in_spec_are_attempted_and_recorded(tmp_path):
    specs = (
        ("scenario_one", _always_fail("first failure")),
        ("scenario_two", _always_pass({"ok": True})),
        ("scenario_three", _always_pass({"ok": True})),
    )
    result = _run(repo_root=tmp_path, scenario_specs=specs)
    assert set(result.evidence.scenarios.keys()) == {"scenario_one", "scenario_two", "scenario_three"}
    assert result.evidence.scenarios["scenario_one"]["status"] == "FAIL"
    assert result.evidence.scenarios["scenario_two"]["status"] == "PASS"
    assert result.evidence.scenarios["scenario_three"]["status"] == "PASS"


# --- evidence contents ----------------------------------------------------


def test_evidence_json_has_schema_version_1_and_required_fields(tmp_path):
    result = _run(
        repo_root=tmp_path,
        environment_builder=_fake_environment_builder(run_id="rschematest01", git_sha="cafef00dbeefdead"),
        scenario_specs=(("fake_ok", _always_pass({"event_id": "abc-123"})),),
    )
    doc = json.loads(result.json_path.read_text())
    assert doc["schema_version"] == 1
    assert doc["run"]["run_id"] == "rschematest01"
    assert doc["run"]["git_sha"] == "cafef00dbeefdead"
    assert doc["run"]["central_profile"] == "linuxlab.settings.central"
    assert doc["run"]["node_profile"] == "linuxlab.settings.node"
    assert doc.get("run", {}).get("started_at")
    assert doc.get("run", {}).get("finished_at")
    assert doc["overall"] == "PASS"
    assert doc["exit_code"] == 0
    assert "fake_ok" in doc["scenarios"]
    assert doc["validation"] == _OK_VALIDATION


def test_evidence_proofs_extracted_from_scenario_details(tmp_path):
    """Allowlisted extraction only (Task 11 corrective Section 7):
    specific named fields, not an arbitrary passthrough dict."""
    result = _run(
        repo_root=tmp_path,
        scenario_specs=(
            (
                "happy_path",
                _always_pass(
                    {
                        "package_sha256": "a" * 64,
                        "public_key_fingerprint": "b" * 64,
                        "signature_verified": True,
                        "outbox_to_inbox": "delivered",
                        "not_a_proof_field": "must not appear in proofs",
                    }
                ),
            ),
            ("n1", _always_pass({"payload_sha256": "c" * 64})),
        ),
    )
    proofs = result.evidence.proofs
    assert proofs["package_sha256"] == "a" * 64
    assert proofs["public_key_fingerprint"] == "b" * 64
    assert proofs["signature_verified"] is True
    assert proofs["outbox_to_inbox"] == "delivered"
    assert proofs["payload_sha256"] == "c" * 64
    assert "not_a_proof_field" not in proofs


def test_evidence_markdown_is_generated(tmp_path):
    result = _run(repo_root=tmp_path)
    assert result.markdown_path.exists()
    text = result.markdown_path.read_text()
    assert "fake_ok" in text
    assert "PASS" in text
    assert "Validation" in text
    assert "Proofs" in text


def test_evidence_dir_that_already_exists_is_a_precheck_failure(tmp_path):
    builder = _fake_environment_builder(run_id="rcollisiontest")
    env_probe = builder(repo_root=tmp_path)
    env_probe.artifact_dir.mkdir(parents=True)

    result = _run(repo_root=tmp_path, environment_builder=builder)
    assert result.overall == "PRECHECK_FAILURE"
    assert result.exit_code == 2


# --- sanitization: the evidence security boundary --------------------------


def test_evidence_never_serializes_raw_exception_text_only_type_name(tmp_path):
    """Task 11 corrective, Critical finding #1: evidence must come from
    an explicit safe FailureRecord (category/stage/code/safe_message
    built from a fixed template), never str(exc)/repr(exc) directly --
    proven here with an answer-key-style sentinel embedded INSIDE the
    exception's own message, which was never pre-registered as a known
    secret. It must be structurally absent, not merely redacted."""

    def leaky_scenario(deployment):
        raise AssertionError(f"unexpected grading result: correct_option_id={SENTINEL_ANSWER_KEY}")

    result = _run(repo_root=tmp_path, scenario_specs=(("leaky", leaky_scenario),))

    assert result.overall == "SCENARIO_FAILURE"
    json_text = result.json_path.read_text()
    markdown_text = result.markdown_path.read_text()
    assert SENTINEL_ANSWER_KEY not in json_text
    assert SENTINEL_ANSWER_KEY not in markdown_text
    # The only exception-derived content ever admitted is the type name.
    assert result.evidence.primary_failure["code"] == "AssertionError"
    assert "AssertionError" in json_text


def test_evidence_never_serializes_sensitive_stdin_payload_from_exception_text(tmp_path):
    def leaky_scenario(deployment):
        raise RuntimeError(f"cli.py action failed with stdin={SENTINEL_STDIN_PAYLOAD}")

    result = _run(repo_root=tmp_path, scenario_specs=(("leaky", leaky_scenario),))

    json_text = result.json_path.read_text()
    markdown_text = result.markdown_path.read_text()
    assert SENTINEL_STDIN_PAYLOAD not in json_text
    assert SENTINEL_STDIN_PAYLOAD not in markdown_text


def test_evidence_never_serializes_a_sentinel_from_a_startup_failure_message(tmp_path):
    """Critical leak test (Task 11 corrective Section 13): a sentinel
    embedded in a STARTUP-stage exception (dual_deployment() setup
    itself failing, before any scenario body ever runs) must be
    structurally absent from evidence too, not just scenario-stage
    exceptions."""
    sentinel = "sentinel-startup-failure-detail-22cd"
    result = _run(
        repo_root=tmp_path,
        dual_deployment_factory=_make_factory(raise_on_enter=StartupError(f"readiness probe failed: {sentinel}")),
    )
    assert result.overall == "STARTUP_FAILURE"
    json_text = result.json_path.read_text()
    markdown_text = result.markdown_path.read_text()
    assert sentinel not in json_text
    assert sentinel not in markdown_text
    assert result.evidence.primary_failure["code"] == "StartupError"


def test_evidence_never_serializes_a_sentinel_from_a_cleanup_only_failure_message(tmp_path):
    """Critical leak test (Task 11 corrective Section 13): a sentinel
    embedded in a CLEANUP-only exception message (the scenario body
    itself succeeded) must be structurally absent from evidence too."""
    sentinel = "sentinel-cleanup-failure-detail-91fe"
    result = _run(
        repo_root=tmp_path,
        dual_deployment_factory=_make_factory(
            raise_on_exit=CleanupAggregateError([("drop_node_database", RuntimeError(sentinel))])
        ),
    )
    assert result.overall == "CLEANUP_FAILURE"
    json_text = result.json_path.read_text()
    markdown_text = result.markdown_path.read_text()
    assert sentinel not in json_text
    assert sentinel not in markdown_text
    assert result.evidence.cleanup_failure["code"] == "CleanupAggregateError"


def test_node_secret_in_exception_text_is_redacted_from_evidence(tmp_path):
    def leaky_scenario(deployment):
        raise AssertionError(f"unexpected response containing {deployment.node_secret}")

    result = _run(
        repo_root=tmp_path,
        dual_deployment_factory=_make_factory(deployment=_FakeDeployment(node_secret=SENTINEL_NODE_SECRET)),
        scenario_specs=(("leaky", leaky_scenario),),
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

    result = _run(
        repo_root=tmp_path,
        dual_deployment_factory=_make_factory(
            deployment=_FakeDeployment(node_secret=SENTINEL_NODE_SECRET, private_key_path=key_path)
        ),
        scenario_specs=(("leaky", leaky_scenario),),
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
    it must still come out redacted (defense in depth, on top of the
    primary boundary of never serializing raw exception text at all)."""
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

    result = _run(repo_root=tmp_path, environment_builder=builder, scenario_specs=(("leaky", leaky_scenario),))
    assert result.overall == "SCENARIO_FAILURE"
    json_text = result.json_path.read_text()
    markdown_text = result.markdown_path.read_text()
    assert sentinel_password not in json_text
    assert sentinel_password not in markdown_text


def test_preflight_exception_never_serializes_raw_text(tmp_path):
    def _failing_builder(*, repo_root, keep_on_failure=False):
        raise RuntimeError(f"unexpected preflight failure embedding {SENTINEL_ANSWER_KEY}")

    result = _run(repo_root=tmp_path, environment_builder=_failing_builder)
    assert result.overall == "PRECHECK_FAILURE"
    assert result.evidence.primary_failure["code"] == "RuntimeError"
    assert SENTINEL_ANSWER_KEY not in result.evidence.primary_failure["safe_message"]


# --- keep-on-failure retained resources ------------------------------------


def test_keep_on_failure_records_retained_resources_in_evidence(tmp_path):
    fake_workspace = tmp_path / "gate-workspace-rko00000001_n2"
    fake_workspace.mkdir()
    fake_deployment = _FakeDeployment(
        tmp_dir=fake_workspace, gate_names=_FakeGateNames("rko00000001_n2")
    )

    result = _run(
        repo_root=tmp_path,
        keep_on_failure=True,
        environment_builder=_fake_environment_builder(run_id="rko00000001"),
        dual_deployment_factory=_make_factory(deployment=fake_deployment),
        scenario_specs=(("n2", _always_fail("simulated failure with retained state")),),
    )
    assert result.overall == "SCENARIO_FAILURE"
    retained = result.evidence.retained_resources
    assert "n2" in retained
    assert retained["n2"]["central_database"] == "linuxlab_gate_central_rko00000001_n2"
    assert retained["n2"]["node_database"] == "linuxlab_gate_node_rko00000001_n2"
    assert retained["n2"]["workspace_path"] == str(fake_workspace)
    assert result.evidence.cleanup_status == "retained_for_debug"


def test_no_retained_resources_when_keep_on_failure_is_false(tmp_path):
    result = _run(
        repo_root=tmp_path,
        keep_on_failure=False,
        scenario_specs=(("n2", _always_fail("simulated failure")),),
    )
    assert result.evidence.retained_resources == {}
    assert result.evidence.cleanup_status == "clean"


def test_successful_run_retains_nothing_even_with_keep_on_failure_true(tmp_path):
    result = _run(repo_root=tmp_path, keep_on_failure=True, scenario_specs=(("fake_ok", _always_pass()),))
    assert result.overall == "PASS"
    assert result.evidence.retained_resources == {}
    assert result.evidence.cleanup_status == "clean"


# --- no auto-tag / auto-commit / auto-push --------------------------------


def test_gate_runner_never_shells_out_to_git_tag_commit_or_push():
    """Structural proof: this module orchestrates the release gate but
    must never create a git tag, commit, or push -- that remains a
    human-controlled action outside Task 11's scope."""
    source = inspect.getsource(gate_runner)
    for forbidden in ("git tag", "git commit", "git push", '"tag"', "'tag'"):
        assert forbidden not in source
