"""Task 15 Task 11 (+ corrective v2): release-gate orchestration --
preflight, the required scenario sequence (each in its own real
dual_deployment(), exactly mirroring how the Task 8-10 pytest suite
already isolates them), evidence assembly, cleanup, and the approved
exit-code mapping.

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
unexpected exception is classified by the explicit STAGE it occurred in
(Task 11 corrective v2, Section 5/16 -- tracked via the `Stage` values
below, set BEFORE the operation belonging to that stage runs, never
inferred after the fact from exception class or context-manager
entry alone): PRECHECK (environment construction, platform authority,
clean-tree, evidence-destination reservation, validation checks) ->
PRECHECK_FAILURE; STARTUP (dual_deployment() setup, before a scenario
body runs) -> STARTUP_FAILURE; SCENARIO (a scenario body) ->
SCENARIO_FAILURE; CLEANUP (teardown-only) -> CLEANUP_FAILURE; FINALIZE
(typed proof/assertion construction, evidence rendering, secret
scanning, transactional publication) -> also CLEANUP_FAILURE/exit 5,
since FINALIZE is an internal stage with no exit code of its own
(Section 5.1).

Failure precedence (Section 5.2): a single scenario's body AND its own
cleanup can both fail -- the body's own classification (STARTUP_FAILURE
or SCENARIO_FAILURE, whichever applies) remains primary; the cleanup
failure is still recorded separately (evidence.cleanup_failure), never
dropped. Across the whole run, the WORST category observed among all
attempted scenarios (STARTUP_FAILURE > SCENARIO_FAILURE >
CLEANUP_FAILURE > PASS) becomes `overall`. A finalization failure NEVER
downgrades or replaces an already-worse primary failure; it only
upgrades a would-be PASS to CLEANUP_FAILURE.

Evidence security boundary (Critical finding #1): raw exception text
(`str(exc)`/`repr(exc)`) is NEVER serialized into evidence, console
output, or any retained artifact. Only a FailureRecord built from an
explicit safe template (category + stage + the exception's TYPE NAME
only) is ever recorded -- see _sanitize_exception(). Known long-lived
per-run secrets (Node bearer secret, DB admin password, private key
PEM) are still registered with SecretRegistry as defense in depth for
any OTHER text that does get rendered (proofs, validation status, etc).

Execution resources are NEVER retained (Task 11 corrective v2, Section
3): dual_deployment() always fully cleans up its own databases,
workspace, and private key, on every exit path, regardless of
--keep-on-failure or failure stage. The flag is recorded in
evidence.retention purely as an audit fact (`keep_on_failure_requested`)
-- it no longer changes what dual_deployment() does. The only thing
that may ever persist from a failed run is the sanitized evidence pair
itself (evidence.retention.sanitized_debug_bundle_retained).
"""

import os
import platform
import shutil
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
from scripts.release_gate.e2e_support import dual_deployment as real_dual_deployment
from scripts.release_gate.environment import (
    GateEnvironment,
    PreflightError,
    assert_linux_platform,
    build_gate_environment,
    compute_authority,
)
from scripts.release_gate.evidence import (
    EXIT_CODE,
    SCHEMA_VERSION,
    FailureRecord,
    InvalidProofError,
    ReleaseEvidence,
    ScenarioResult,
    SecretRegistry,
    StagingCleanupError,
    build_typed_assertions,
    build_typed_proofs,
    publish_evidence,
    validate_for_pass,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

CENTRAL_SETTINGS_MODULE = "linuxlab.settings.central"
NODE_SETTINGS_MODULE = "linuxlab.settings.node"


class Stage:
    """Task 11 corrective v2, Section 5: the explicit gate-run state
    machine. A stage is set BEFORE the operation belonging to it runs,
    never inferred after the fact from context-manager entry or
    exception class alone."""

    PRECHECK = "PRECHECK"
    STARTUP = "STARTUP"
    SCENARIO = "SCENARIO"
    CLEANUP = "CLEANUP"
    FINALIZE = "FINALIZE"
    DONE = "DONE"


# Worst-first. Used to fold each scenario's own category into one
# overall run classification -- see module docstring.
_SEVERITY = {"PASS": 0, "CLEANUP_FAILURE": 1, "SCENARIO_FAILURE": 2, "STARTUP_FAILURE": 3}

# Short, DB-identifier-safe codes for deriving each scenario's own CHILD
# run_id from the outer RunContext's run_id (Section 4: "the same
# run_id must drive... every ephemeral resource").
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


def _run_one_scenario(outer_run_id, name, scenario_func, scenario_tmp_dir, dual_deployment_factory, secrets):
    """Runs exactly one scenario in its own dual_deployment(), using a
    CHILD run_id derived from `outer_run_id` (Section 4: traceable
    identity, never an unrelated random one). Returns (ScenarioResult,
    category_or_None, primary_FailureRecord_or_None,
    cleanup_FailureRecord_or_None, detail_value_or_None). `category` is
    None on success, else one of "STARTUP_FAILURE"/"SCENARIO_FAILURE"/
    "CLEANUP_FAILURE".

    Explicit stage tracking (Section 5/16, hardened by corrective v4
    Section 6.4): `stage` starts at Stage.STARTUP and is advanced to
    Stage.SCENARIO only once dual_deployment_factory(...) has actually
    yielded a deployment -- an exception before that point is a startup
    failure by construction, never inferred from exception type. Once
    the scenario body (and typed assertion construction, Section 8)
    fully completes, `stage` is advanced ONE FURTHER to Stage.CLEANUP
    before the `with` block's own suite ends -- the only code that can
    still run after that point is `__exit__` (teardown). This means
    that IF `outcome_exception` is set below, checking `stage ==
    Stage.CLEANUP` alone proves it came from teardown, for ANY
    exception class whatsoever -- never by checking whether it happens
    to be a `CleanupAggregateError` specifically (Section 6.4: "do not
    depend on CleanupAggregateError specifically to recognize cleanup
    stage"). A malformed assertion value fails the scenario itself
    (never reaches Stage.CLEANUP), using the exact same precedence
    handling as any other body failure.

    The scenario body's own exception is deliberately let propagate
    NATURALLY through `dual_deployment_factory(...)`'s own `finally`
    (never caught inside the `with` block itself): it lets
    dual_deployment() correctly observe via `sys.exc_info()` that the
    run failed, and if cleanup ALSO then raises, Python's own implicit
    exception chaining sets the new exception's `__context__` to the
    original body/setup exception for free -- checked generically here
    (`__context__ is not None`), again never by testing the new
    exception's class."""
    child_run_id = _child_run_id(outer_run_id, name)
    start = time.monotonic()
    stage = Stage.STARTUP
    outcome_exception = None
    detail_value = None
    assertions: dict = {}

    try:
        with dual_deployment_factory(scenario_tmp_dir, run_id=child_run_id) as deployment:
            stage = Stage.SCENARIO
            _register_deployment_secrets(deployment, secrets)
            detail_value = scenario_func(deployment)
            assertions = build_typed_assertions(detail_value)
            stage = Stage.CLEANUP
    except Exception as exc:  # noqa: BLE001 -- classified below, never silently dropped
        outcome_exception = exc

    duration_ms = (time.monotonic() - start) * 1000.0

    if outcome_exception is None:
        return (
            ScenarioResult(name=name, status="PASS", assertions=assertions, duration_ms=duration_ms, detail=""),
            None,
            None,
            None,
            detail_value,
        )

    if stage == Stage.CLEANUP:
        # Setup and the scenario body both fully succeeded -- whatever
        # this exception's actual class is, it can only have come from
        # __exit__ (teardown).
        cleanup_record = _sanitize_exception(outcome_exception, name, "CLEANUP_FAILURE", secrets)
        category = "CLEANUP_FAILURE"
        record = cleanup_record
    else:
        # Setup or the scenario body itself failed. If teardown ALSO
        # then failed while that original exception was unwinding,
        # Python's own implicit chaining set `__context__` to it.
        original_exception = outcome_exception.__context__
        category = "SCENARIO_FAILURE" if stage == Stage.SCENARIO else "STARTUP_FAILURE"
        if original_exception is not None:
            cleanup_record = _sanitize_exception(outcome_exception, name, "CLEANUP_FAILURE", secrets)
            record = _sanitize_exception(original_exception, name, category, secrets)
        else:
            cleanup_record = None
            record = _sanitize_exception(outcome_exception, name, category, secrets)

    return (
        ScenarioResult(name=name, status="FAIL", duration_ms=duration_ms, detail=record.safe_message),
        category,
        record,
        cleanup_record,
        None,
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
    """Gate-level validation (Section 12): real Central/Node
    `manage.py check` and migration-drift status, run once per gate
    run, independent of any specific scenario's deployment. A PASS must
    never carry `validation = {}` -- every key here is always
    populated with a real (categorical, never raw command output)
    status string."""
    central_check = _run_manage_command(env, CENTRAL_SETTINGS_MODULE, ["check"])
    node_check = _run_manage_command(env, NODE_SETTINGS_MODULE, ["check"])
    central_drift = _run_manage_command(env, CENTRAL_SETTINGS_MODULE, ["makemigrations", "--check", "--dry-run"])
    node_drift = _run_manage_command(env, NODE_SETTINGS_MODULE, ["makemigrations", "--check", "--dry-run"])
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
    metadata = {"platform": platform.system().lower(), "python_version": platform.python_version()}
    try:
        metadata["django_version"] = django.get_version()
    except Exception:  # noqa: BLE001 -- best-effort environment metadata only
        metadata["django_version"] = "unknown"
    try:
        connect_kwargs = (
            {"host": db_admin.host, "port": db_admin.port, "user": db_admin.user, "password": db_admin.password}
            if db_admin is not None
            else {}
        )
        with psycopg.connect(dbname="postgres", connect_timeout=3, **connect_kwargs) as conn:
            metadata["postgres_version"] = str(conn.info.server_version)
    except Exception:  # noqa: BLE001 -- best-effort environment metadata only
        metadata["postgres_version"] = "unknown"
    return metadata


def _finish(
    *,
    run_id,
    git_sha,
    started_at,
    authority,
    overall,
    scenario_results,
    detail_values,
    primary_failure,
    cleanup_failure,
    cleanup_status,
    validation,
    secrets,
    evidence_dir,
    keep_on_failure,
    db_admin=None,
) -> GateRunResult:
    """Task 11 corrective v2/v3/v4, Section 13/16: the FINALIZE stage.
    Typed proof/assertion construction happens here for proofs
    (assertions are already built per-scenario in _run_one_scenario --
    see that function's docstring for why); the canonical EvidenceV1
    candidate is built; then, ONLY if nothing has failed it so far
    (`overall == "PASS"`), the AUTHORITATIVE `evidence.validate_for_
    pass()` is consulted as the single gate a candidate must clear to
    be minted PASS (Section 3: "PASS is not a default state"). This is
    a direct, hardcoded call -- corrective v4, Section 3.1: production
    `run_release_gate()`/`_finish()` accept no injectable validator/
    callback/hook capable of deciding PASS eligibility; tests that need
    to exercise a genuine PASS path must satisfy this exact function,
    never substitute for it. If it says the candidate is not eligible,
    `overall` is downgraded to CLEANUP_FAILURE/exit 5 with a
    `finalization_failure` built from the validator's own safe reason
    codes, never from raw candidate content. Only after that gate does
    publish_evidence() attempt the all-or-nothing publish, so the
    persisted JSON/Markdown pair always agrees with the decision. A
    failure in ANY of these steps becomes `finalization_failure`: if
    `overall` was still PASS, it is upgraded to CLEANUP_FAILURE/exit 5
    (Section 5.1 -- FINALIZE has no exit code of its own); if a worse
    primary failure already exists, that primary's category/exit code
    is left untouched (Section 5.2) and the finalization failure is
    only ever additional information, never a replacement."""
    finished_at = _utc_now_iso()

    finalization_failure = None
    try:
        proofs = build_typed_proofs(*detail_values.values())
    except InvalidProofError as exc:
        proofs = {}
        finalization_failure = _sanitize_exception(exc, "finalize_proofs", "CLEANUP_FAILURE", secrets)

    if finalization_failure is not None and overall == "PASS":
        overall = "CLEANUP_FAILURE"

    # Predicted True whenever publication will actually be attempted --
    # corrected back to False below if that attempt fails. This value
    # must be decided BEFORE the evidence is rendered/published, since
    # the persisted JSON/Markdown pair has to agree with what is
    # actually on disk the moment it is written (Section 12: JSON and
    # Markdown parity extends to matching their own retained state).
    retention = {
        "keep_on_failure_requested": keep_on_failure,
        "execution_resources_retained": False,
        "sanitized_debug_bundle_retained": evidence_dir is not None,
    }

    evidence = ReleaseEvidence(
        schema_version=SCHEMA_VERSION,
        run={
            "run_id": run_id,
            "git_sha": git_sha,
            "started_at": started_at,
            "finished_at": finished_at,
            "authority": authority.__dict__,
        },
        environment=_environment_metadata(db_admin),
        profiles={"central": CENTRAL_SETTINGS_MODULE, "node": NODE_SETTINGS_MODULE},
        validation=validation,
        scenarios={result.name: result.__dict__ for result in scenario_results},
        proofs=proofs,
        retention=retention,
        primary_failure=primary_failure.to_dict() if primary_failure is not None else None,
        cleanup_failure=cleanup_failure.to_dict() if cleanup_failure is not None else None,
        finalization_failure=finalization_failure.to_dict() if finalization_failure is not None else None,
        cleanup_status=cleanup_status,
        overall=overall,
        exit_code=EXIT_CODE[overall],
    )

    # Section 3: PASS is not a default state -- the ONLY way `overall`
    # may remain "PASS" past this point is if the real, hardcoded
    # evidence.validate_for_pass() actually certifies this exact
    # candidate. Only consulted when nothing else has already failed
    # the run, so it can only ever GATE a would-be PASS, never override
    # an already-worse primary/cleanup failure.
    if evidence.overall == "PASS":
        decision = validate_for_pass(evidence)
        if not decision.eligible:
            safe_message = secrets.redact_text("; ".join(decision.reasons))
            evidence.finalization_failure = FailureRecord(
                category="CLEANUP_FAILURE",
                stage="finalize_pass_validation",
                code="EvidenceInvariantError",
                safe_message=safe_message,
            ).to_dict()
            evidence.overall = "CLEANUP_FAILURE"
            evidence.exit_code = EXIT_CODE["CLEANUP_FAILURE"]

    json_path = None
    markdown_path = None
    if evidence_dir is not None:
        try:
            json_path, markdown_path = publish_evidence(evidence_dir, evidence, secrets)
        except Exception as exc:  # noqa: BLE001 -- publication must never crash the gate itself
            evidence.retention["sanitized_debug_bundle_retained"] = False
            if isinstance(exc, StagingCleanupError):
                # Corrective v4, Section 28: publication AND the
                # subsequent staging-directory cleanup BOTH failed --
                # genuine residue was left on disk. Split into two
                # distinct records: the staging-cleanup failure itself
                # (cleanup_failure, and cleanup_status genuinely
                # reflects it) and the ORIGINAL publication failure
                # (preserved as `__cause__`, never overwritten) as
                # finalization_failure.
                if evidence.cleanup_failure is None:
                    evidence.cleanup_failure = _sanitize_exception(
                        exc, "finalize_publish_staging_cleanup", "CLEANUP_FAILURE", secrets
                    ).to_dict()
                evidence.cleanup_status = "failed"
                original_exc = exc.__cause__
                if original_exc is not None and evidence.finalization_failure is None:
                    evidence.finalization_failure = _sanitize_exception(
                        original_exc, "finalize_publish", "CLEANUP_FAILURE", secrets
                    ).to_dict()
            elif evidence.finalization_failure is None:
                evidence.finalization_failure = _sanitize_exception(
                    exc, "finalize_publish", "CLEANUP_FAILURE", secrets
                ).to_dict()
            if evidence.overall == "PASS":
                evidence.overall = "CLEANUP_FAILURE"
                evidence.exit_code = EXIT_CODE["CLEANUP_FAILURE"]

    return GateRunResult(
        overall=evidence.overall,
        exit_code=evidence.exit_code,
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

    Outermost fail-safe boundary (Section 9/16): this function never
    raises for ANY failure, expected or not. Every stage is wrapped so
    an unanticipated exception is still classified by the stage it
    occurred in and still produces sanitized evidence when that remains
    possible, rather than propagating a raw traceback to the caller.

    Authority (Section 10) is computed from the ACTUAL running platform
    unconditionally, independent of `check_platform` -- a Darwin
    development run (`check_platform=False`) may still PASS
    functionally, but its evidence always self-identifies as
    non-authoritative."""
    repo_root = Path(repo_root)
    started_at = _utc_now_iso()
    secrets = SecretRegistry()
    authority = compute_authority(platform_name=platform.system())
    stage = Stage.PRECHECK

    # --- PRECHECK, part 1: platform authority + environment construction.
    # A failure here means the run's own identity was never established,
    # so there is no safe evidence destination to write to at all.
    try:
        if check_platform:
            assert_linux_platform()
        env: GateEnvironment = environment_builder(repo_root=repo_root, keep_on_failure=keep_on_failure)
    except Exception as exc:  # noqa: BLE001 -- classified below, never silently dropped, never raw
        record = _sanitize_exception(exc, "preflight", "PRECHECK_FAILURE", secrets)
        return _finish(
            run_id="unknown",
            git_sha="unknown",
            started_at=started_at,
            authority=authority,
            overall="PRECHECK_FAILURE",
            scenario_results=[ScenarioResult(name="preflight", status="FAIL", detail=record.safe_message)],
            detail_values={},
            primary_failure=record,
            cleanup_failure=None,
            cleanup_status="clean",
            validation={},
            secrets=secrets,
            evidence_dir=None,
            keep_on_failure=keep_on_failure,
        )

    secrets.register(env.db_admin.password)

    # --- PRECHECK, part 2: evidence destination reservation + validation.
    # The run's identity IS known now (Section 17: "preflight failure can
    # still produce safe minimal evidence when the target is available").
    validation: dict = {}
    try:
        if env.artifact_dir.exists():
            raise PreflightError(f"release evidence directory already exists: {env.artifact_dir}")
        validation = run_validation(env)
        if not _validation_ok(validation):
            raise PreflightError("validation checks did not pass (see validation section for status)")
    except Exception as exc:  # noqa: BLE001 -- classified below, never silently dropped, never raw
        record = _sanitize_exception(exc, stage, "PRECHECK_FAILURE", secrets)
        return _finish(
            run_id=env.run_id,
            git_sha=env.git_sha,
            started_at=started_at,
            authority=authority,
            overall="PRECHECK_FAILURE",
            scenario_results=[ScenarioResult(name=stage, status="FAIL", detail=record.safe_message)],
            detail_values={},
            primary_failure=record,
            cleanup_failure=None,
            cleanup_status="clean",
            validation=validation,
            secrets=secrets,
            evidence_dir=env.artifact_dir,
            keep_on_failure=keep_on_failure,
        )

    scenario_results: list = []
    detail_values: dict = {}
    cleanup_records: list = []
    # Tracks the actual FailureRecord PER scenario/stage (not just its
    # category) so the run-level `primary_failure` can be the record
    # whose OWN category matches the worst `overall` category observed
    # -- never a mismatched reconstruction.
    failure_records: list = []
    overall = "PASS"

    tmp_root_path = Path(
        tempfile.mkdtemp(prefix=f"phase1-release-{env.run_id}-", dir=str(tmp_root) if tmp_root is not None else None)
    )
    try:
        for name, scenario_func in scenario_specs:
            stage = name
            scenario_tmp_dir = tmp_root_path / name
            scenario_tmp_dir.mkdir(parents=True, exist_ok=True)
            result, category, primary_record, cleanup_record, detail_value = _run_one_scenario(
                env.run_id, name, scenario_func, scenario_tmp_dir, dual_deployment_factory, secrets
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
    except Exception as exc:  # noqa: BLE001 -- unexpected failure OUTSIDE any single scenario's own handling
        record = _sanitize_exception(exc, stage, "STARTUP_FAILURE", secrets)
        failure_records.append(record)
        if _SEVERITY["STARTUP_FAILURE"] > _SEVERITY[overall]:
            overall = "STARTUP_FAILURE"
        scenario_results.append(ScenarioResult(name=stage, status="FAIL", detail=record.safe_message))
    finally:
        # Nothing is ever retained under the run's own outer temp root
        # any more (execution resources are never retained -- Section
        # 3), so it is always safe to remove it unconditionally here.
        # Corrective v3, Section 20: cleanup is a release invariant, not
        # best-effort -- a failure here is recorded as a genuine
        # cleanup failure (making cleanup_status != clean and PASS
        # impossible) rather than silently ignored via
        # ignore_errors=True.
        try:
            shutil.rmtree(tmp_root_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_records.append(
                _sanitize_exception(exc, "outer_temp_root_cleanup", "CLEANUP_FAILURE", secrets)
            )

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
    if cleanup_failure is not None:
        cleanup_status = "failed"
        if overall == "PASS":
            overall = "CLEANUP_FAILURE"

    return _finish(
        run_id=env.run_id,
        git_sha=env.git_sha,
        started_at=started_at,
        authority=authority,
        overall=overall,
        scenario_results=scenario_results,
        detail_values=detail_values,
        primary_failure=primary_failure,
        cleanup_failure=cleanup_failure,
        cleanup_status=cleanup_status,
        validation=validation,
        secrets=secrets,
        evidence_dir=env.artifact_dir,
        keep_on_failure=keep_on_failure,
        db_admin=env.db_admin,
    )
