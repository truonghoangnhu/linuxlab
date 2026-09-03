"""Task 15 Task 11: release-gate orchestration -- preflight, the
required scenario sequence (each in its own real dual_deployment(),
exactly mirroring how the Task 8-10 pytest suite already isolates
them), evidence assembly, cleanup, and the approved exit-code mapping.

Deliberately separated from scripts/phase1_release_gate.py (the thin
argv/CLI layer) so this orchestration is independently testable without
going through subprocess/argv parsing -- and deliberately
dependency-injectable (`scenario_specs`, `dual_deployment_factory`,
`environment_builder`) so ORCHESTRATION correctness (exit-code mapping,
failure precedence, evidence sanitization) can be proven with fast
fakes, reserving real Postgres/Gunicorn cycles for the small number of
genuine end-to-end proof tests.

Exit-code / failure-category mapping is exactly
scripts.release_gate.evidence.EXIT_CODE, per the approved Task 15
design spec Section 18 ("Failure Model"):

    0 = PASS
    2 = PRECHECK_FAILURE  (environment/repo/dependency/db-safety/config)
    3 = STARTUP_FAILURE   (db migration, Gunicorn startup, readiness)
    4 = SCENARIO_FAILURE  (happy-path/negative/recovery/invariant failure)
    5 = CLEANUP_FAILURE   (required cleanup cannot be completed)

There is no distinct "internal/unexpected error" category in the
approved spec -- an unexpected exception raised while a scenario is
executing (after its own dual_deployment() successfully started) is,
per the spec's own classification text ("a happy-path, negative-path,
recovery, or release invariant fails"), a SCENARIO_FAILURE; an
unexpected exception raised before any scenario's dual_deployment()
ever starts is a STARTUP_FAILURE. This module does not invent a sixth
category.

Failure precedence when a single scenario's body AND its own cleanup
both fail: the body's classification (STARTUP_FAILURE or
SCENARIO_FAILURE) remains the scenario's primary category -- matching
spec Section 18's "preserves the primary failure classification
deterministically; records cleanup failures separately if both occur"
-- while the cleanup failure text is still recorded, never dropped, in
the scenario's own detail and in the run's aggregated cleanup_failure
field. Across the whole run, the WORST category observed among all
attempted scenarios (STARTUP_FAILURE > SCENARIO_FAILURE >
CLEANUP_FAILURE > PASS) becomes `overall`.
"""

import platform
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import django
import psycopg

from scripts.release_gate import scenarios as scenario_module
from scripts.release_gate.cleanup import CleanupAggregateError
from scripts.release_gate.e2e_support import dual_deployment as real_dual_deployment
from scripts.release_gate.environment import (
    GateEnvironment,
    PreflightError,
    assert_linux_platform,
    build_gate_environment,
)
from scripts.release_gate.evidence import (
    EXIT_CODE,
    SCHEMA_VERSION,
    ReleaseEvidence,
    ScenarioResult,
    SecretRegistry,
    write_json_evidence,
    write_markdown_evidence,
)

# Worst-first. Used to fold each scenario's own category into one
# overall run classification -- see module docstring.
_SEVERITY = {"PASS": 0, "CLEANUP_FAILURE": 1, "SCENARIO_FAILURE": 2, "STARTUP_FAILURE": 3}

DEFAULT_SCENARIO_SPECS = (
    ("happy_path", scenario_module.run_happy_path),
    ("n1_tampered_replacement", scenario_module.run_n1_tampered_replacement),
    ("n2_revoked_credential", scenario_module.run_n2_revoked_credential),
    ("n3_ack_lost_real_proxy", scenario_module.run_n3_ack_lost_real_proxy),
    ("n4_central_unavailable", scenario_module.run_n4_central_unavailable),
    ("n5_node_restart", scenario_module.run_n5_node_restart),
    ("backup_restore_smoke", scenario_module.run_backup_restore_smoke),
)


@dataclass
class GateRunResult:
    overall: str
    exit_code: int
    evidence: ReleaseEvidence | None
    evidence_dir: Path | None
    json_path: Path | None
    markdown_path: Path | None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sanitize_exception(exc: BaseException, secrets: SecretRegistry) -> str:
    return secrets.redact_text(f"{type(exc).__name__}: {exc}")


def _safe_detail_repr(detail_value) -> str:
    return "" if not detail_value else str(detail_value)


def _register_deployment_secrets(deployment, secrets: SecretRegistry) -> None:
    node_secret = getattr(deployment, "node_secret", None)
    if node_secret:
        secrets.register(node_secret)
    private_key_path = getattr(deployment, "private_key_path", None)
    if private_key_path is not None:
        path = Path(private_key_path)
        if path.exists():
            secrets.register(path.read_text())


def _run_one_scenario(name, scenario_func, scenario_tmp_dir, dual_deployment_factory, secrets, keep_on_failure=False):
    """Runs exactly one scenario in its own dual_deployment(). Returns
    (ScenarioResult, category_or_None, cleanup_detail_or_None,
    artifact_detail_or_None). `category` is None on success, else one
    of "STARTUP_FAILURE"/"SCENARIO_FAILURE"/"CLEANUP_FAILURE" -- see
    module docstring for the precedence rule when both a body failure
    and a cleanup failure occur for the same scenario.

    The scenario body's own exception is deliberately let propagate
    NATURALLY through `dual_deployment_factory(...)`'s own `finally`
    (never caught inside this function's `with` block) for two reasons:
    it lets `dual_deployment(keep_on_failure=...)` correctly observe via
    `sys.exc_info()` that the run failed, and if cleanup ALSO then
    raises, Python's own implicit exception chaining sets the new
    exception's `__context__` to the original body exception for free
    -- exactly the signal used below to prove BOTH failures occurred
    without losing either one."""
    start = time.monotonic()
    entered = False
    outcome_exception = None
    detail_value = None

    try:
        with dual_deployment_factory(scenario_tmp_dir, keep_on_failure=keep_on_failure) as deployment:
            entered = True
            _register_deployment_secrets(deployment, secrets)
            detail_value = scenario_func(deployment)
    except Exception as exc:  # noqa: BLE001 -- classified below, never silently dropped
        outcome_exception = exc

    duration_ms = (time.monotonic() - start) * 1000.0

    if outcome_exception is None:
        return (
            ScenarioResult(name=name, status="PASS", duration_ms=duration_ms, detail=""),
            None,
            None,
            detail_value,
        )

    cleanup_detail = None
    if isinstance(outcome_exception, CleanupAggregateError):
        cleanup_detail = _sanitize_exception(outcome_exception, secrets)
        original_body_exception = outcome_exception.__context__
        if entered and original_body_exception is not None:
            # Both the scenario body AND its own cleanup failed -- the
            # body's classification remains primary (spec Section 18:
            # "preserves the primary failure classification
            # deterministically"); the cleanup failure is still
            # surfaced via `cleanup_detail`, never dropped.
            category = "SCENARIO_FAILURE"
            primary_text = _sanitize_exception(original_body_exception, secrets)
        else:
            category = "CLEANUP_FAILURE"
            primary_text = cleanup_detail
    elif entered:
        category = "SCENARIO_FAILURE"
        primary_text = _sanitize_exception(outcome_exception, secrets)
    else:
        category = "STARTUP_FAILURE"
        primary_text = _sanitize_exception(outcome_exception, secrets)

    return (
        ScenarioResult(name=name, status="FAIL", duration_ms=duration_ms, detail=primary_text),
        category,
        cleanup_detail,
        None,
    )


def _environment_checks(db_admin=None) -> dict:
    checks = {"platform": platform.system().lower(), "python": platform.python_version()}
    try:
        checks["django"] = django.get_version()
    except Exception:  # noqa: BLE001 -- best-effort environment metadata only
        checks["django"] = "unknown"
    try:
        connect_kwargs = (
            {"host": db_admin.host, "port": db_admin.port, "user": db_admin.user, "password": db_admin.password}
            if db_admin is not None
            else {}
        )
        with psycopg.connect(dbname="postgres", connect_timeout=3, **connect_kwargs) as conn:
            checks["postgresql"] = conn.info.server_version
    except Exception:  # noqa: BLE001 -- best-effort environment metadata only
        checks["postgresql"] = "unknown"
    return checks


def _finish(
    *,
    run_id,
    git_sha,
    started_at,
    overall,
    scenario_results,
    cleanup_failure,
    secrets,
    checks,
    artifacts,
    evidence_dir,
) -> GateRunResult:
    finished_at = _utc_now_iso()
    evidence = ReleaseEvidence(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        git_sha=git_sha,
        started_at=started_at,
        finished_at=finished_at,
        environment=checks,
        checks={},
        scenarios={result.name: result.__dict__ for result in scenario_results},
        artifacts=artifacts,
        overall=overall,
        cleanup_failure=cleanup_failure,
    )

    json_path = None
    markdown_path = None
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        json_path = evidence_dir / "release-evidence.json"
        markdown_path = evidence_dir / "release-evidence.md"
        write_json_evidence(json_path, evidence, secrets)
        write_markdown_evidence(markdown_path, evidence, secrets)

    return GateRunResult(
        overall=overall,
        exit_code=EXIT_CODE[overall],
        evidence=evidence,
        evidence_dir=evidence_dir,
        json_path=json_path,
        markdown_path=markdown_path,
    )


def run_release_gate(
    *,
    repo_root,
    keep_on_failure: bool = False,
    check_platform: bool = True,
    scenario_specs=DEFAULT_SCENARIO_SPECS,
    dual_deployment_factory=real_dual_deployment,
    environment_builder=build_gate_environment,
    tmp_root=None,
) -> GateRunResult:
    """Runs the full release gate: preflight, every scenario in
    `scenario_specs` (each in its own fresh dual_deployment(), never a
    single deployment shared across all of them -- exactly how the
    Task 8-10 pytest suite already isolates them), evidence assembly,
    and returns the result. Never raises for an expected gate/scenario
    outcome -- only a genuinely unrecoverable precondition (e.g. a bad
    `repo_root`) could raise before evidence can be written at all, and
    even that is treated as PRECHECK_FAILURE here rather than
    propagating."""
    repo_root = Path(repo_root)
    started_at = _utc_now_iso()
    secrets = SecretRegistry()

    try:
        if check_platform:
            assert_linux_platform()
        env: GateEnvironment = environment_builder(repo_root=repo_root, keep_on_failure=keep_on_failure)
        if env.artifact_dir.exists():
            raise PreflightError(f"release evidence directory already exists: {env.artifact_dir}")
    except PreflightError as exc:
        return _finish(
            run_id="unknown",
            git_sha="unknown",
            started_at=started_at,
            overall="PRECHECK_FAILURE",
            scenario_results=[ScenarioResult(name="preflight", status="FAIL", detail=str(exc))],
            cleanup_failure=None,
            secrets=secrets,
            checks=_environment_checks(),
            artifacts={},
            evidence_dir=None,
        )

    secrets.register(env.db_admin.password)

    scenario_results = []
    artifacts = {}
    cleanup_failures = []
    overall = "PASS"

    with tempfile.TemporaryDirectory(
        prefix=f"phase1-release-{env.run_id}-", dir=str(tmp_root) if tmp_root is not None else None
    ) as tmp_root_str:
        tmp_root_path = Path(tmp_root_str)
        for name, scenario_func in scenario_specs:
            scenario_tmp_dir = tmp_root_path / name
            scenario_tmp_dir.mkdir(parents=True, exist_ok=True)
            result, category, cleanup_detail, detail_value = _run_one_scenario(
                name, scenario_func, scenario_tmp_dir, dual_deployment_factory, secrets, keep_on_failure
            )
            scenario_results.append(result)
            if detail_value:
                artifacts[name] = _summarize_detail(detail_value)
            if category is not None and _SEVERITY[category] > _SEVERITY[overall]:
                overall = category
            if cleanup_detail is not None:
                cleanup_failures.append(f"{name}: {cleanup_detail}")

    cleanup_failure_text = "; ".join(cleanup_failures) if cleanup_failures else None
    if cleanup_failure_text is not None and overall == "PASS":
        overall = "CLEANUP_FAILURE"

    return _finish(
        run_id=env.run_id,
        git_sha=env.git_sha,
        started_at=started_at,
        overall=overall,
        scenario_results=scenario_results,
        cleanup_failure=cleanup_failure_text,
        secrets=secrets,
        checks=_environment_checks(env.db_admin),
        artifacts=artifacts,
        evidence_dir=env.artifact_dir,
    )


def _summarize_detail(detail_value) -> dict:
    """Scenario `detail` dicts are already evidence-safe (built by
    scripts.release_gate.scenarios, which never returns secret/answer-
    key material) -- passed through as-is so evidence keeps concrete
    facts (event ids, package SHA-256, scores) rather than an opaque
    string."""
    return dict(detail_value) if isinstance(detail_value, dict) else {"detail": str(detail_value)}
