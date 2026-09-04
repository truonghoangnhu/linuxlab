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

There is no distinct "internal/unexpected error" category. An
unexpected exception is classified by the STAGE where it occurred:
preflight/environment construction/validation checks -> PRECHECK_
FAILURE; infrastructure startup before a scenario body runs ->
STARTUP_FAILURE; a scenario body -> SCENARIO_FAILURE; cleanup-only ->
CLEANUP_FAILURE.

Failure precedence (Task 11 corrective, Important finding #2): when a
single scenario's body AND its own cleanup both fail, the body's own
classification (STARTUP_FAILURE or SCENARIO_FAILURE -- whichever
applies) remains primary; the cleanup failure is still recorded
separately (evidence.cleanup_failure), never dropped. Across the whole
run, the WORST category observed among all attempted scenarios
(STARTUP_FAILURE > SCENARIO_FAILURE > CLEANUP_FAILURE > PASS) becomes
`overall`.

Evidence security boundary (Task 11 corrective, Critical finding #1):
raw exception text (`str(exc)`/`repr(exc)`) is NEVER serialized into
evidence, console output, or any retained artifact. Only a
FailureRecord built from an explicit safe template (category + stage +
the exception's TYPE NAME only) is ever recorded -- see
_sanitize_exception(). This is deliberately robust against an
unanticipated sensitive value (an answer-key sentinel, a stdin
payload) appearing inside some exception's own message: since that
message is never read for evidence purposes at all, there is no
reliance on having pre-registered that specific value as a known
secret. Known long-lived per-run secrets (Node bearer secret, DB admin
password, private key PEM) are still registered with SecretRegistry as
defense in depth for any OTHER text that does get rendered (proofs,
validation status, etc).
"""

import os
import platform
import subprocess
import sys
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
    FailureRecord,
    ReleaseEvidence,
    ScenarioResult,
    SecretRegistry,
    write_json_evidence,
    write_markdown_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Worst-first. Used to fold each scenario's own category into one
# overall run classification -- see module docstring.
_SEVERITY = {"PASS": 0, "CLEANUP_FAILURE": 1, "SCENARIO_FAILURE": 2, "STARTUP_FAILURE": 3}

# Allowlisted evidence.proofs fields (design spec Section 17.1 / Task 11
# corrective Section 7): explicit fields only, never an arbitrary
# scenario-returned dict passed through wholesale.
_PROOF_KEYS = (
    "package_sha256",
    "payload_sha256",
    "public_key_fingerprint",
    "signature_verified",
    "outbox_to_inbox",
)

# Short, DB-identifier-safe codes for deriving each scenario's own CHILD
# run_id from the outer RunContext's run_id (Task 11 corrective Section
# 4/10: "the same run_id must drive... every ephemeral resource").
_SCENARIO_RUN_CODES = {
    "happy_path": "hp",
    "n1_tampered_replacement": "n1",
    "n2_revoked_credential": "n2",
    "n3_ack_lost_real_proxy": "n3",
    "n4_central_unavailable": "n4",
    "n5_node_restart": "n5",
    "backup_restore_smoke": "br",
}

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


def _child_run_id(outer_run_id: str, scenario_name: str) -> str:
    code = _SCENARIO_RUN_CODES.get(scenario_name, scenario_name[:2])
    return f"{outer_run_id}_{code}"


def _sanitize_exception(exc: BaseException, stage: str, category: str, secrets: SecretRegistry) -> FailureRecord:
    """The ONLY place an exception is turned into evidence content.
    `code` (the exception's class name) is the sole exception-derived
    fact ever used -- never `str(exc)`/`repr(exc)`, which could echo
    back arbitrary/sensitive content an assertion message happened to
    embed. `safe_message` is a fixed template over already-safe
    strings (category/stage/code), then redacted as defense in depth
    even though none of its inputs should ever contain a registered
    secret by construction."""
    code = type(exc).__name__
    safe_message = secrets.redact_text(f"{code} raised during {stage}")
    return FailureRecord(category=category, stage=stage, code=code, safe_message=safe_message)


def _register_deployment_secrets(deployment, secrets: SecretRegistry) -> None:
    node_secret = getattr(deployment, "node_secret", None)
    if node_secret:
        secrets.register(node_secret)
    private_key_path = getattr(deployment, "private_key_path", None)
    if private_key_path is not None:
        path = Path(private_key_path)
        if path.exists():
            secrets.register(path.read_text())


def _extract_proofs(detail_values: dict) -> dict:
    """Allowlisted extraction only (Task 11 corrective Section 7): pull
    the specific named proof fields out of whichever scenario detail
    dicts provided them -- never pass an arbitrary scenario-returned
    dict through wholesale."""
    proofs: dict = {}
    for detail in detail_values.values():
        if not isinstance(detail, dict):
            continue
        for key in _PROOF_KEYS:
            if key in detail and key not in proofs:
                proofs[key] = detail[key]
    return proofs


def _run_one_scenario(
    outer_run_id, name, scenario_func, scenario_tmp_dir, dual_deployment_factory, secrets, keep_on_failure=False
):
    """Runs exactly one scenario in its own dual_deployment(), using a
    CHILD run_id derived from `outer_run_id` (Task 11 corrective
    Section 4: traceable identity, never an unrelated random one).
    Returns (ScenarioResult, category_or_None, primary_FailureRecord_
    or_None, cleanup_FailureRecord_or_None, detail_value_or_None,
    retained_resources_or_None). `category` is None on success, else
    one of "STARTUP_FAILURE"/"SCENARIO_FAILURE"/"CLEANUP_FAILURE".

    The scenario body's own exception is deliberately let propagate
    NATURALLY through `dual_deployment_factory(...)`'s own `finally`
    (never caught inside this function's `with` block): it lets
    `dual_deployment(keep_on_failure=...)` correctly observe via
    `sys.exc_info()` that the run failed, and if cleanup ALSO then
    raises, Python's own implicit exception chaining sets the new
    exception's `__context__` to the original body/setup exception for
    free -- the signal used below to prove BOTH failures occurred
    without losing either one (Task 11 corrective, Important finding
    #2: this check no longer depends on whether the scenario body ever
    started -- a startup failure followed by a cleanup failure now
    correctly remains STARTUP_FAILURE, not CLEANUP_FAILURE)."""
    child_run_id = _child_run_id(outer_run_id, name)
    start = time.monotonic()
    entered = False
    outcome_exception = None
    detail_value = None
    captured_deployment = None

    try:
        with dual_deployment_factory(
            scenario_tmp_dir, keep_on_failure=keep_on_failure, run_id=child_run_id
        ) as deployment:
            entered = True
            captured_deployment = deployment
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
            None,
            detail_value,
            None,
        )

    cleanup_record = None
    if isinstance(outcome_exception, CleanupAggregateError):
        cleanup_record = _sanitize_exception(outcome_exception, name, "CLEANUP_FAILURE", secrets)
        original_exception = outcome_exception.__context__
        if original_exception is not None:
            # Both the scenario's own setup/body AND its cleanup failed
            # -- the ORIGINAL failure's classification remains primary
            # regardless of whether it happened before (STARTUP) or
            # after (SCENARIO) the body started; the cleanup failure is
            # still surfaced via `cleanup_record`, never dropped.
            category = "SCENARIO_FAILURE" if entered else "STARTUP_FAILURE"
            record = _sanitize_exception(original_exception, name, category, secrets)
        else:
            category = "CLEANUP_FAILURE"
            record = cleanup_record
    elif entered:
        category = "SCENARIO_FAILURE"
        record = _sanitize_exception(outcome_exception, name, category, secrets)
    else:
        category = "STARTUP_FAILURE"
        record = _sanitize_exception(outcome_exception, name, category, secrets)

    retained = None
    if keep_on_failure and captured_deployment is not None:
        tmp_dir = getattr(captured_deployment, "tmp_dir", None)
        if tmp_dir is not None and Path(tmp_dir).exists():
            gate_names = captured_deployment.gate_names
            retained = {
                "central_database": gate_names.central,
                "node_database": gate_names.node,
                "workspace_path": str(tmp_dir),
            }

    return (
        ScenarioResult(name=name, status="FAIL", duration_ms=duration_ms, detail=record.safe_message),
        category,
        record,
        cleanup_record,
        None,
        retained,
    )


def _run_manage_command(env: GateEnvironment, django_settings_module: str, args: list) -> subprocess.CompletedProcess:
    """Runs `manage.py <args>` for one profile. Deliberately points
    POSTGRES_DB at the universal `postgres` maintenance database --
    always present on a working PostgreSQL server, never a dev/prod/
    gate-owned database -- rather than at any real application schema,
    since `check`/`makemigrations --check` compare in-memory model
    state against migration files on disk and do not require the
    application's own tables to exist to produce an accurate result."""
    child_env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": django_settings_module,
        "POSTGRES_DB": "postgres",
        "POSTGRES_HOST": env.db_admin.host,
        "POSTGRES_PORT": env.db_admin.port,
        "POSTGRES_USER": env.db_admin.user,
        "POSTGRES_PASSWORD": env.db_admin.password,
    }
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=str(env.repo_root),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _run_validation_checks(env: GateEnvironment) -> dict:
    """Gate-level validation (Task 11 corrective Section 12): real
    Central/Node `manage.py check` and migration-drift status, run
    once per gate run, independent of any specific scenario's
    deployment. A PASS must never carry `validation = {}` -- every key
    here is always populated with a real (categorical, never raw
    command output) status string."""
    central_check = _run_manage_command(env, "linuxlab.settings.central", ["check"])
    node_check = _run_manage_command(env, "linuxlab.settings.node", ["check"])
    central_drift = _run_manage_command(
        env, "linuxlab.settings.central", ["makemigrations", "--check", "--dry-run"]
    )
    node_drift = _run_manage_command(env, "linuxlab.settings.node", ["makemigrations", "--check", "--dry-run"])
    return {
        "clean_tree": True,  # reaching this point already required build_gate_environment()'s own clean-tree check
        "central_check": "ok" if central_check.returncode == 0 else "failed",
        "node_check": "ok" if node_check.returncode == 0 else "failed",
        "central_migration_drift": "none" if central_drift.returncode == 0 else "detected",
        "node_migration_drift": "none" if node_drift.returncode == 0 else "detected",
    }


def _validation_ok(validation: dict) -> bool:
    return (
        validation.get("central_check") == "ok"
        and validation.get("node_check") == "ok"
        and validation.get("central_migration_drift") == "none"
        and validation.get("node_migration_drift") == "none"
    )


def _environment_metadata(db_admin=None) -> dict:
    metadata = {"platform": platform.system().lower(), "python": platform.python_version()}
    try:
        metadata["django"] = django.get_version()
    except Exception:  # noqa: BLE001 -- best-effort environment metadata only
        metadata["django"] = "unknown"
    try:
        connect_kwargs = (
            {"host": db_admin.host, "port": db_admin.port, "user": db_admin.user, "password": db_admin.password}
            if db_admin is not None
            else {}
        )
        with psycopg.connect(dbname="postgres", connect_timeout=3, **connect_kwargs) as conn:
            metadata["postgresql"] = conn.info.server_version
    except Exception:  # noqa: BLE001 -- best-effort environment metadata only
        metadata["postgresql"] = "unknown"
    return metadata


def _finish(
    *,
    run_id,
    git_sha,
    started_at,
    platform_name,
    overall,
    scenario_results,
    proofs,
    retained_resources,
    primary_failure,
    cleanup_failure,
    cleanup_status,
    validation,
    secrets,
    evidence_dir,
) -> GateRunResult:
    finished_at = _utc_now_iso()
    evidence = ReleaseEvidence(
        schema_version=SCHEMA_VERSION,
        run={
            "run_id": run_id,
            "git_sha": git_sha,
            "started_at": started_at,
            "finished_at": finished_at,
            "platform": platform_name,
            "central_profile": "linuxlab.settings.central",
            "node_profile": "linuxlab.settings.node",
        },
        validation=validation,
        scenarios={result.name: result.__dict__ for result in scenario_results},
        proofs=proofs,
        retained_resources=retained_resources,
        primary_failure=primary_failure.to_dict() if primary_failure is not None else None,
        cleanup_failure=cleanup_failure.to_dict() if cleanup_failure is not None else None,
        cleanup_status=cleanup_status,
        overall=overall,
        exit_code=EXIT_CODE[overall],
    )

    json_path = None
    markdown_path = None
    # Fail-safe evidence finalization (Task 11 corrective Section 9):
    # attempted even when nothing else succeeded, but never allowed to
    # publish unsafe output -- write_json_evidence()/write_markdown_
    # evidence() themselves raise SecretLeakError rather than write a
    # partially-redacted artifact, and that failure must not crash the
    # gate itself.
    if evidence_dir is not None:
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            candidate_json = evidence_dir / "release-evidence.json"
            candidate_markdown = evidence_dir / "release-evidence.md"
            write_json_evidence(candidate_json, evidence, secrets)
            write_markdown_evidence(candidate_markdown, evidence, secrets)
            json_path = candidate_json
            markdown_path = candidate_markdown
        except Exception:  # noqa: BLE001 -- evidence writing must never crash the gate itself
            json_path = None
            markdown_path = None

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
    run_validation=_run_validation_checks,
    tmp_root=None,
) -> GateRunResult:
    """Runs the full release gate: preflight -> validation checks ->
    every scenario in `scenario_specs` (each in its own fresh
    dual_deployment() using a run_id DERIVED from the one RunContext
    identity established here, never a single deployment shared across
    all of them, and never an unrelated random identity either) ->
    evidence assembly -> returns the result.

    Outermost fail-safe boundary (Task 11 corrective Section 9): this
    function never raises for ANY failure, expected or not. Every
    stage is wrapped so an unanticipated exception is still classified
    by the stage it occurred in and still produces sanitized evidence
    when that remains possible, rather than propagating a raw
    traceback to the caller."""
    repo_root = Path(repo_root)
    started_at = _utc_now_iso()
    secrets = SecretRegistry()
    stage = "preflight"
    validation: dict = {}

    try:
        if check_platform:
            assert_linux_platform()
        env: GateEnvironment = environment_builder(repo_root=repo_root, keep_on_failure=keep_on_failure)
        if env.artifact_dir.exists():
            raise PreflightError(f"release evidence directory already exists: {env.artifact_dir}")
        secrets.register(env.db_admin.password)

        stage = "validation"
        validation = run_validation(env)
        if not _validation_ok(validation):
            raise PreflightError("validation checks did not pass (see validation section for status)")
    except Exception as exc:  # noqa: BLE001 -- classified below, never silently dropped, never raw
        record = _sanitize_exception(exc, stage, "PRECHECK_FAILURE", secrets)
        return _finish(
            run_id="unknown",
            git_sha="unknown",
            started_at=started_at,
            platform_name=platform.system().lower(),
            overall="PRECHECK_FAILURE",
            scenario_results=[ScenarioResult(name=stage, status="FAIL", detail=record.safe_message)],
            proofs={},
            retained_resources={},
            primary_failure=record,
            cleanup_failure=None,
            cleanup_status="clean",
            validation=validation,
            secrets=secrets,
            evidence_dir=None,
        )

    scenario_results: list = []
    detail_values: dict = {}
    retained_resources: dict = {}
    cleanup_records: list = []
    # Tracks the actual FailureRecord PER scenario/stage (not just its
    # category) so the run-level `primary_failure` can be the record
    # whose OWN category matches the worst `overall` category observed
    # -- never a mismatched reconstruction (Task 11 corrective,
    # Important finding #2 also covers this: a later, more severe
    # failure must not be described using an earlier, less severe
    # scenario's own message).
    failure_records: list = []
    overall = "PASS"

    try:
        with tempfile.TemporaryDirectory(
            prefix=f"phase1-release-{env.run_id}-", dir=str(tmp_root) if tmp_root is not None else None
        ) as tmp_root_str:
            tmp_root_path = Path(tmp_root_str)
            for name, scenario_func in scenario_specs:
                stage = name
                scenario_tmp_dir = tmp_root_path / name
                scenario_tmp_dir.mkdir(parents=True, exist_ok=True)
                result, category, primary_record, cleanup_record, detail_value, retained = _run_one_scenario(
                    env.run_id, name, scenario_func, scenario_tmp_dir, dual_deployment_factory, secrets,
                    keep_on_failure,
                )
                scenario_results.append(result)
                if detail_value:
                    detail_values[name] = detail_value
                if category is not None:
                    failure_records.append(primary_record)
                    if _SEVERITY[category] > _SEVERITY[overall]:
                        overall = category
                if cleanup_record is not None:
                    cleanup_records.append(cleanup_record)
                if retained is not None:
                    retained_resources[name] = retained
    except Exception as exc:  # noqa: BLE001 -- unexpected failure OUTSIDE any single scenario's own handling
        record = _sanitize_exception(exc, stage, "STARTUP_FAILURE", secrets)
        failure_records.append(record)
        if _SEVERITY["STARTUP_FAILURE"] > _SEVERITY[overall]:
            overall = "STARTUP_FAILURE"
        scenario_results.append(ScenarioResult(name=stage, status="FAIL", detail=record.safe_message))

    # The primary_failure is the recorded FailureRecord whose OWN
    # category matches `overall` -- the worst one observed -- never an
    # arbitrary "first failure" that might describe a less severe issue
    # under a more severe label.
    primary_failure = next((record for record in failure_records if record.category == overall), None)

    cleanup_failure = cleanup_records[0] if cleanup_records else None
    if len(cleanup_records) > 1:
        combined = "; ".join(record.safe_message for record in cleanup_records)
        cleanup_failure = FailureRecord(
            category="CLEANUP_FAILURE", stage="multiple", code="CleanupAggregateError", safe_message=combined
        )
    cleanup_status = "clean"
    if retained_resources:
        cleanup_status = "retained_for_debug"
    if cleanup_failure is not None:
        cleanup_status = "failed"
        if overall == "PASS":
            overall = "CLEANUP_FAILURE"

    return _finish(
        run_id=env.run_id,
        git_sha=env.git_sha,
        started_at=started_at,
        platform_name=platform.system().lower(),
        overall=overall,
        scenario_results=scenario_results,
        proofs=_extract_proofs(detail_values),
        retained_resources=retained_resources,
        primary_failure=primary_failure,
        cleanup_failure=cleanup_failure,
        cleanup_status=cleanup_status,
        validation=validation,
        secrets=secrets,
        evidence_dir=env.artifact_dir,
    )
