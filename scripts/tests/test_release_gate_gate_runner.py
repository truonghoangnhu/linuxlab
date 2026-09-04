"""Task 15 Task 11 (+ corrective v2): release-gate orchestration -- exit-
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

Corrective v2 supersedes the prior --keep-on-failure execution-resource
retention model entirely (Section 3: execution resources are NEVER
retained) -- `retained_resources` no longer exists in evidence; see
`evidence.retention` instead. It also adds typed proofs/assertions
(Section 7/8, proven directly in test_release_gate_evidence_typed_
proofs.py / test_release_gate_evidence_typed_assertions.py and here at
the orchestration-boundary level only), an explicit finalization stage
(Section 5), and authority metadata (Section 10, proven here and in
test_release_gate_authority.py).
"""

import inspect
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from scripts.release_gate import gate_runner
from scripts.release_gate.cleanup import CleanupAggregateError
from scripts.release_gate.environment import GateEnvironment, PreflightError
from scripts.release_gate.evidence import PassDecision
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


def _fake_pass_validator(candidate) -> PassDecision:
    """Task 15 Task 11 corrective v3: the REAL validate_for_pass()
    requires the exact seven real scenario names with their exact
    typed assertion sets -- meaningless for THIS file's fast fakes,
    which use synthetic scenario names/details to isolate ORCHESTRATION
    correctness (exit codes, precedence, sanitization) from real
    scenario content. This lightweight stand-in checks only the
    structural invariants orchestration tests actually care about
    (no recorded failure, clean cleanup, no retained execution
    resources) -- exhaustive real-invariant coverage lives in
    test_release_gate_evidence_pass_validator.py and the real-infra
    CLI verification, not here."""
    reasons = []
    if candidate.primary_failure is not None:
        reasons.append("primary_failure_present")
    if candidate.cleanup_failure is not None:
        reasons.append("cleanup_failure_present")
    if candidate.finalization_failure is not None:
        reasons.append("finalization_failure_present")
    if candidate.cleanup_status != "clean":
        reasons.append("cleanup_status_not_clean")
    if candidate.retention.get("execution_resources_retained") is not False:
        reasons.append("execution_resources_retained")
    return PassDecision(eligible=not reasons, reasons=tuple(reasons))


def _run(**overrides):
    kwargs = {
        "repo_root": overrides.pop("repo_root"),
        "check_platform": False,
        "environment_builder": _fake_environment_builder(),
        "dual_deployment_factory": _make_factory(),
        "scenario_specs": (("fake_ok", _always_pass()),),
        "run_validation": _fake_run_validation(),
        "pass_validator": _fake_pass_validator,
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
    assert result.evidence_dir is None, "precheck failure before environment resolution must not attempt evidence"


def test_failed_validation_check_is_a_precheck_failure(tmp_path):
    """Design spec Section 12 / FAST CI parity: a PASS must never carry
    real validation failures silently -- Central/Node manage.py check
    or migration drift failing blocks the release the same way a dirty
    tree does."""
    result = _run(
        repo_root=tmp_path,
        run_validation=_fake_run_validation({**_OK_VALIDATION, "central_migration_drift": "detected"}),
    )
    assert result.overall == "PRECHECK_FAILURE"
    assert result.exit_code == 2


def test_precheck_failure_after_environment_resolved_still_publishes_evidence(tmp_path):
    """Corrective v2, Section 17: a preflight failure that happens
    AFTER the run's own identity/evidence destination is known (e.g. a
    failed validation check, as opposed to environment_builder() itself
    raising) must still produce a real, safe evidence pair -- it is not
    acceptable to silently drop evidence just because the run never
    reached a scenario."""
    result = _run(
        repo_root=tmp_path,
        run_validation=_fake_run_validation({**_OK_VALIDATION, "central_migration_drift": "detected"}),
    )
    assert result.overall == "PRECHECK_FAILURE"
    assert result.evidence_dir is not None
    assert result.json_path is not None
    assert result.json_path.exists()
    assert result.markdown_path is not None
    assert result.markdown_path.exists()


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


def test_keep_on_failure_flag_is_not_threaded_to_the_deployment_factory(tmp_path):
    """Corrective v2, Section 3: execution resources are never
    retained, so dual_deployment() no longer needs (or accepts) a
    keep_on_failure argument at all -- run_release_gate() must not pass
    one through, regardless of what the caller requested."""
    observed = []

    def recording_factory(tmp_dir, **kwargs):
        observed.append(kwargs)
        return _fake_deployment_cm(tmp_dir, kwargs.get("run_id"), None, None, None)

    _run(repo_root=tmp_path, keep_on_failure=True, dual_deployment_factory=recording_factory)
    assert len(observed) == 1
    assert "keep_on_failure" not in observed[0]


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
    assert "authoritative" in doc["run"]["authority"]
    assert doc["profiles"]["central"] == "linuxlab.settings.central"
    assert doc["profiles"]["node"] == "linuxlab.settings.node"
    assert doc.get("run", {}).get("started_at")
    assert doc.get("run", {}).get("finished_at")
    assert "environment" in doc
    assert doc["overall"] == "PASS"
    assert doc["exit_code"] == 0
    assert "fake_ok" in doc["scenarios"]
    assert doc["validation"] == _OK_VALIDATION
    assert doc["retention"]["execution_resources_retained"] is False
    assert doc["retention"]["sanitized_debug_bundle_retained"] is True


def test_evidence_proofs_extracted_and_typed_from_scenario_details(tmp_path):
    """Corrective v2, Section 7: allowlisted AND typed extraction --
    specific named fields, each validated against its own typed schema,
    never an arbitrary passthrough dict."""
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
                        "outbox_to_inbox": {
                            "event_uuid": "12345678-1234-5678-1234-567812345678",
                            "inbox_count": 1,
                            "node_state": "DELIVERED",
                            "duplicate_idempotent": True,
                        },
                        "not_a_proof_field": "must not appear in proofs",
                    }
                ),
            ),
            ("n1", _always_pass({"payload_sha256": "c" * 64})),
        ),
    )
    assert result.overall == "PASS"
    proofs = result.evidence.proofs
    assert proofs["package_sha256"] == "a" * 64
    assert proofs["public_key_fingerprint"] == "b" * 64
    assert proofs["signature"] == {"verified": True}
    assert proofs["outbox_to_inbox"]["node_state"] == "DELIVERED"
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


# --- retention: execution resources never retained (corrective v2) --------


def test_retention_records_the_flag_but_never_claims_execution_resources_retained(tmp_path):
    result = _run(
        repo_root=tmp_path,
        keep_on_failure=True,
        scenario_specs=(("n2", _always_fail("simulated failure")),),
    )
    assert result.overall == "SCENARIO_FAILURE"
    retention = result.evidence.retention
    assert retention["keep_on_failure_requested"] is True
    assert retention["execution_resources_retained"] is False
    assert retention["sanitized_debug_bundle_retained"] is True


def test_retention_reflects_false_when_not_requested(tmp_path):
    result = _run(
        repo_root=tmp_path,
        keep_on_failure=False,
        scenario_specs=(("n2", _always_fail("simulated failure")),),
    )
    retention = result.evidence.retention
    assert retention["keep_on_failure_requested"] is False
    assert retention["execution_resources_retained"] is False


def test_successful_run_never_claims_execution_resources_retained_even_with_flag_true(tmp_path):
    result = _run(repo_root=tmp_path, keep_on_failure=True, scenario_specs=(("fake_ok", _always_pass()),))
    assert result.overall == "PASS"
    assert result.evidence.retention["execution_resources_retained"] is False


def test_evidence_never_carries_a_retained_resources_field_any_more(tmp_path):
    result = _run(repo_root=tmp_path)
    doc = json.loads(result.json_path.read_text())
    assert "retained_resources" not in doc


# --- explicit stage classification (corrective v2, Section 16) ------------


def test_finalization_failure_from_malformed_proof_is_cleanup_failure_exit_5(tmp_path):
    """Section 18.1/18.2: a scenario returning a malformed proof value
    (here, an uppercase/wrong-format sha256) must never be silently
    admitted into evidence -- it becomes a finalization failure, and the
    run cannot PASS."""
    result = _run(
        repo_root=tmp_path,
        scenario_specs=(("fake_ok", _always_pass({"package_sha256": "NOT-A-VALID-SHA256"})),),
    )
    assert result.overall == "CLEANUP_FAILURE"
    assert result.exit_code == 5
    assert result.evidence.finalization_failure is not None
    assert result.evidence.finalization_failure["code"] == "InvalidProofError"
    json_text = result.json_path.read_text()
    assert "NOT-A-VALID-SHA256" not in json_text


def test_finalization_failure_never_overrides_an_existing_worse_primary_failure(tmp_path):
    """A malformed proof from one scenario must not silently replace a
    genuine scenario failure's own exit code -- the scenario failure
    stays primary; the finalization failure is still recorded."""
    result = _run(
        repo_root=tmp_path,
        scenario_specs=(
            ("fake_fail", _always_fail("simulated body failure")),
            ("fake_bad_proof", _always_pass({"package_sha256": "NOT-A-VALID-SHA256"})),
        ),
    )
    assert result.overall == "SCENARIO_FAILURE"
    assert result.exit_code == 4
    assert result.evidence.primary_failure["category"] == "SCENARIO_FAILURE"
    assert result.evidence.finalization_failure is not None
    assert result.evidence.finalization_failure["code"] == "InvalidProofError"


def test_malformed_assertion_value_fails_the_owning_scenario(tmp_path):
    """Section 8: an owning scenario that returns a malformed typed
    assertion value (here, a non-bool for a bool-typed assertion key)
    must itself be treated as a scenario failure -- never silently
    coerced or dropped."""
    result = _run(
        repo_root=tmp_path,
        scenario_specs=(("n1", _always_pass({"existing_active_preserved": "not-a-bool"})),),
    )
    assert result.overall == "SCENARIO_FAILURE"
    assert result.exit_code == 4
    assert result.evidence.primary_failure["code"] == "InvalidProofError"


def test_scenario_result_carries_typed_assertions(tmp_path):
    result = _run(
        repo_root=tmp_path,
        scenario_specs=(
            (
                "n1",
                _always_pass(
                    {
                        "existing_active_preserved": True,
                        "active_package_identity_unchanged": True,
                        "replacement_rejected": True,
                        "event_id": "not-an-assertion-field",
                    }
                ),
            ),
        ),
    )
    assert result.overall == "PASS"
    assertions = result.evidence.scenarios["n1"]["assertions"]
    assert assertions == {
        "existing_active_preserved": True,
        "active_package_identity_unchanged": True,
        "replacement_rejected": True,
    }


# --- authority metadata (corrective v2, Section 10) ------------------------


def test_authority_is_authoritative_when_platform_is_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_runner.platform, "system", lambda: "Linux")
    result = _run(repo_root=tmp_path)
    assert result.evidence.run["authority"]["authoritative"] is True
    assert result.evidence.run["authority"]["platform"] == "linux"
    assert result.evidence.run["authority"]["reason"] is None


def test_authority_is_non_authoritative_on_darwin_even_when_functionally_passing(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_runner.platform, "system", lambda: "Darwin")
    result = _run(repo_root=tmp_path)
    assert result.overall == "PASS"
    assert result.evidence.run["authority"]["authoritative"] is False
    assert result.evidence.run["authority"]["platform"] == "darwin"
    assert result.evidence.run["authority"]["reason"] == "development_only_platform"


# --- fail-closed cleanup (corrective v3, Section 20/21/28.5) --------------


def test_outer_temp_root_cleanup_failure_prevents_pass_and_clean_status(tmp_path, monkeypatch):
    """Section 20: cleanup is a release invariant, not best-effort --
    a failure removing the run's own outer temp root must be recorded,
    must make cleanup_status != clean, and must make PASS impossible,
    never silently ignored via ignore_errors=True."""
    import scripts.release_gate.gate_runner as gate_runner_module

    original_rmtree = gate_runner_module.shutil.rmtree
    calls = {"n": 0}

    def _flaky_rmtree(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated outer temp root cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(gate_runner_module.shutil, "rmtree", _flaky_rmtree)

    result = _run(repo_root=tmp_path)

    assert result.overall != "PASS"
    assert result.exit_code == 5
    assert result.evidence.cleanup_status != "clean"
    assert result.evidence.cleanup_failure is not None


def test_outer_temp_root_cleanup_failure_does_not_override_a_worse_primary_failure(tmp_path, monkeypatch):
    import scripts.release_gate.gate_runner as gate_runner_module

    original_rmtree = gate_runner_module.shutil.rmtree
    calls = {"n": 0}

    def _flaky_rmtree(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated outer temp root cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(gate_runner_module.shutil, "rmtree", _flaky_rmtree)

    result = _run(repo_root=tmp_path, scenario_specs=(("fake_fail", _always_fail()),))

    assert result.overall == "SCENARIO_FAILURE"
    assert result.exit_code == 4
    assert result.evidence.cleanup_status != "clean"
    assert result.evidence.cleanup_failure is not None


def test_publish_evidence_staging_cleanup_failure_is_never_silently_ignored(tmp_path):
    """Section 21: if publication fails before the atomic publish AND
    the subsequent staging-directory cleanup also fails, that must
    still surface as a non-zero, non-clean result -- never silently
    swallowed via ignore_errors=True."""
    import scripts.release_gate.evidence as evidence_module

    source = inspect.getsource(evidence_module.publish_evidence)
    assert "ignore_errors=True" not in source


# --- validate_for_pass() integration (corrective v3, Section 3) -----------


def test_pass_validator_ineligible_downgrades_would_be_pass_to_cleanup_failure(tmp_path):
    """Section 3: PASS is not a default state -- a run where every
    scenario/cleanup step succeeded structurally must still be refused
    if the injected pass_validator says the candidate is not eligible."""

    def _always_ineligible(candidate):
        return PassDecision(eligible=False, reasons=("simulated_ineligible_reason",))

    result = _run(repo_root=tmp_path, pass_validator=_always_ineligible)
    assert result.overall == "CLEANUP_FAILURE"
    assert result.exit_code == 5
    assert result.evidence.finalization_failure is not None
    assert result.evidence.finalization_failure["code"] == "EvidenceInvariantError"
    assert "simulated_ineligible_reason" in result.evidence.finalization_failure["safe_message"]


def test_pass_validator_eligible_allows_pass(tmp_path):
    def _always_eligible(candidate):
        return PassDecision(eligible=True, reasons=())

    result = _run(repo_root=tmp_path, pass_validator=_always_eligible)
    assert result.overall == "PASS"
    assert result.exit_code == 0


def test_pass_validator_is_never_consulted_when_a_scenario_already_failed(tmp_path):
    """The pass_validator only GATES a would-be PASS -- it is not
    consulted at all once a worse primary failure already exists, so it
    can never override that failure's own category/exit code (Section
    5: 'no secondary failure may replace an existing primary failure')."""
    calls = []

    def _recording_validator(candidate):
        calls.append(candidate.overall)
        return PassDecision(eligible=False, reasons=("irrelevant",))

    result = _run(
        repo_root=tmp_path,
        pass_validator=_recording_validator,
        scenario_specs=(("fake_fail", _always_fail()),),
    )
    assert result.overall == "SCENARIO_FAILURE"
    assert result.exit_code == 4
    assert calls == []


def test_real_validate_for_pass_rejects_a_fake_scenario_set_end_to_end(tmp_path):
    """Real orchestration + the REAL evidence.validate_for_pass():
    proves the default wiring actually enforces the exact required
    scenario set, using the fake infra to avoid real Postgres/Gunicorn."""
    from scripts.release_gate.evidence import validate_for_pass

    result = _run(repo_root=tmp_path, pass_validator=validate_for_pass)
    assert result.overall == "CLEANUP_FAILURE"
    assert result.exit_code == 5
    assert result.evidence.finalization_failure is not None
    assert "required_scenario_set_mismatch" in result.evidence.finalization_failure["safe_message"]


def test_default_pass_validator_is_the_real_validate_for_pass():
    import inspect

    default = inspect.signature(gate_runner.run_release_gate).parameters["pass_validator"].default
    from scripts.release_gate.evidence import validate_for_pass

    assert default is validate_for_pass


# --- no auto-tag / auto-commit / auto-push --------------------------------


def test_gate_runner_never_shells_out_to_git_tag_commit_or_push():
    """Structural proof: this module orchestrates the release gate but
    must never create a git tag, commit, or push -- that remains a
    human-controlled action outside Task 11's scope."""
    source = inspect.getsource(gate_runner)
    for forbidden in ("git tag", "git commit", "git push", '"tag"', "'tag'"):
        assert forbidden not in source
