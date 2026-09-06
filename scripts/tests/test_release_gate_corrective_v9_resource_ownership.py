"""Task 15 Task 11 corrective v9, Gap 2 (design spec Sections 11-23):
`ResourceLedger` was only ever active, in real time, for the two
restore databases (`RESTORE_CENTRAL_DB`/`RESTORE_NODE_DB`) -- Central
DB, Node DB, workspace, outer run root, and the evidence staging
directory were all acquired only RETROACTIVELY, after the fact, some of
them only on a specific cleanup-failure branch and never on the
ordinary success path. Ownership for the first three was reconstructed
after each scenario completed by pattern-matching cleanup-exception
ACTION NAMES against a fixed table (`_CLEANUP_ACTION_TO_RESOURCE_KIND`)
-- exactly the anti-pattern spec Section 23 forbids.

These are the mandatory RED tests from the v9 design spec, Section 35,
items 6-12: live ownership during a resource's lifetime (before its own
cleanup runs), partial-startup ownership, and ownership correctness
that does not depend on cleanup-action names.

Real ephemeral PostgreSQL is used throughout for the database/workspace
tests (never a simulated/boolean stand-in), mirroring the existing
scripts/tests/test_release_gate_partial_setup_cleanup.py and
scripts/tests/test_release_gate_backup_restore.py house style.
"""

import uuid

import psycopg
import pytest

from scripts.release_gate import e2e_support, gate_runner
from scripts.release_gate.cleanup import CleanupAggregateError
from scripts.release_gate.e2e_support import db_admin_from_env, dual_deployment
from scripts.release_gate.evidence import (
    SCHEMA_VERSION,
    ReleaseEvidence,
    SecretRegistry,
    publish_evidence,
)


def _database_exists(db_admin, name):
    with psycopg.connect(
        host=db_admin.host,
        port=db_admin.port,
        user=db_admin.user,
        password=db_admin.password,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        return row is not None


def _run_id():
    return "r" + uuid.uuid4().hex[:10]


# --- RED tests 6/7/8: live Central DB / Node DB / workspace ---------------
# --- physical=true and ledger=true DURING the resource's lifetime ---------


def test_central_and_node_db_are_ledger_owned_while_physically_live(tmp_path):
    ledger = gate_runner.ResourceLedger()
    run_id = _run_id()

    with dual_deployment(tmp_path, start_node=False, run_id=run_id, resource_ledger=ledger) as deployment:
        # Physical existence, checked live (before any cleanup runs).
        assert _database_exists(deployment.db_admin, deployment.gate_names.central) is True
        assert _database_exists(deployment.db_admin, deployment.gate_names.node) is True

        # Ledger membership, checked live -- never inferred after the
        # fact from a cleanup-exception's action name.
        assert ledger.contains("central_db", deployment.gate_names.central) is True
        assert ledger.contains("node_db", deployment.gate_names.node) is True

    # After successful teardown, both are verifiably gone AND released.
    assert _database_exists(db_admin_from_env(), gate_runner.GateDatabaseNames.for_run(run_id).central) is False
    assert _database_exists(db_admin_from_env(), gate_runner.GateDatabaseNames.for_run(run_id).node) is False
    assert ledger.contains("central_db", gate_runner.GateDatabaseNames.for_run(run_id).central) is False
    assert ledger.contains("node_db", gate_runner.GateDatabaseNames.for_run(run_id).node) is False


def test_workspace_is_ledger_owned_while_physically_live(tmp_path):
    ledger = gate_runner.ResourceLedger()
    run_id = _run_id()

    with dual_deployment(tmp_path, start_node=False, run_id=run_id, resource_ledger=ledger) as deployment:
        assert deployment.tmp_dir.exists() is True
        assert ledger.contains("workspace", str(deployment.tmp_dir)) is True
        workspace_path = deployment.tmp_dir

    assert workspace_path.exists() is False
    assert ledger.contains("workspace", str(workspace_path)) is False


# --- RED test 9: live outer root physical=true/ledger=true -----------------


def test_outer_root_is_ledger_owned_while_a_scenario_is_executing(tmp_path, monkeypatch):
    """Uses FAKE scenario/dual_deployment/validation callables (no real
    Postgres/Gunicorn needed for this one -- gate_runner's own outer-root
    creation is real regardless) so the scenario body itself can observe
    live ledger/filesystem state for the OUTER root while it is still
    the run's current stage -- i.e. strictly before that root's own
    cleanup (gate_runner._run_release_gate()'s own `finally`) ever
    runs."""
    from contextlib import contextmanager
    from dataclasses import dataclass
    from pathlib import Path

    from scripts.release_gate.environment import DatabaseAdminConnection, GateEnvironment

    captured = {}

    real_mkdtemp = gate_runner.tempfile.mkdtemp

    def _spy_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        captured["tmp_root_path"] = path
        return path

    monkeypatch.setattr(gate_runner.tempfile, "mkdtemp", _spy_mkdtemp)

    @dataclass
    class _FakeDeployment:
        node_secret: str = "sentinel"
        private_key_path: object = None
        tmp_dir: object = None
        resource_ledger: object = None

    @contextmanager
    def _fake_dual_deployment(tmp_dir, **kwargs):
        dep = _FakeDeployment(tmp_dir=Path(tmp_dir))
        yield dep

    def _fake_environment_builder(*, repo_root, keep_on_failure=False):
        run_id = "routerroottest"
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

    def _observing_scenario(deployment):
        # This runs DURING Stage.SCENARIO -- strictly before
        # _run_release_gate()'s own outer-root `finally` cleanup block
        # has run. `deployment.resource_ledger` is the SAME
        # `state.resources` ResourceLedger the whole run shares.
        ledger = deployment.resource_ledger
        tmp_root_path = captured["tmp_root_path"]
        captured["live_physical_exists"] = Path(tmp_root_path).exists()
        captured["live_ledger_owned"] = ledger.contains(gate_runner.ResourceKind.OUTER_ROOT, str(tmp_root_path))
        return {}

    monkeypatch.setattr(gate_runner, "assert_linux_platform", lambda: None)

    result = gate_runner.run_release_gate(
        repo_root=tmp_path,
        check_platform=False,
        scenario_specs=(("only_scenario", _observing_scenario),),
        dual_deployment_factory=_fake_dual_deployment,
        environment_builder=_fake_environment_builder,
        run_validation=_fake_run_validation,
        tmp_root=tmp_path,
    )

    assert captured.get("live_physical_exists") is True
    assert captured.get("live_ledger_owned") is True

    # After the run concludes, the outer root has been removed and
    # released.
    assert Path(captured["tmp_root_path"]).exists() is False
    assert result.stage == gate_runner.Stage.DONE


# --- RED test 10: live staging physical=true/ledger=true -------------------


def _make_minimal_evidence(**overrides):
    fields = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": "rstagingtest1",
            "git_sha": "a" * 40,
            "started_at": "2026-09-06T00:00:00+00:00",
            "finished_at": "2026-09-06T00:05:00+00:00",
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
        "overall": "PASS",
        "exit_code": 0,
    }
    fields.update(overrides)
    return ReleaseEvidence(**fields)


def test_staging_directory_is_ledger_owned_while_physically_live_before_rename(tmp_path, monkeypatch):
    import scripts.release_gate.evidence as evidence_module

    ledger = gate_runner.ResourceLedger()
    evidence_dir = tmp_path / "release-evidence"
    observed = {}

    original_atomic_write = evidence_module._atomic_write_text

    def _observing_atomic_write(path, text):
        # Called while the staging directory physically exists and has
        # NOT yet been atomically renamed into place -- i.e. strictly
        # during the staging directory's own live ownership window,
        # before its own disposal/rename.
        staging_dir = path.parent
        observed["physical_exists"] = staging_dir.exists()
        observed["ledger_owned"] = ledger.contains("staging_dir", "evidence_staging")
        return original_atomic_write(path, text)

    monkeypatch.setattr(evidence_module, "_atomic_write_text", _observing_atomic_write)

    publish_evidence(evidence_dir, _make_minimal_evidence(), SecretRegistry(), resource_ledger=ledger)

    assert observed.get("physical_exists") is True
    assert observed.get("ledger_owned") is True
    # After a successful atomic publish, the staging directory no
    # longer exists under its staging name, and ownership is released.
    assert ledger.contains("staging_dir", "evidence_staging") is False


def test_staging_directory_ownership_survives_publication_failure(tmp_path):
    """If publication fails but staging cleanup succeeds, the ledger
    must never have been left claiming ownership past that verified
    removal -- but if staging cleanup ALSO fails (residue genuinely left
    on disk), ownership must remain (never released for a resource that
    was never verifiably disposed of)."""
    from unittest.mock import patch

    import scripts.release_gate.evidence as evidence_module

    ledger = gate_runner.ResourceLedger()
    evidence_dir = tmp_path / "release-evidence"

    with (
        patch.object(evidence_module, "_atomic_write_text", side_effect=RuntimeError("simulated write failure")),
        patch.object(evidence_module.shutil, "rmtree", side_effect=OSError("simulated staging cleanup failure")),
        pytest.raises(evidence_module.StagingCleanupError),
    ):
        publish_evidence(evidence_dir, _make_minimal_evidence(), SecretRegistry(), resource_ledger=ledger)

    # Genuine residue was left on disk -- ownership must remain.
    assert ledger.contains("staging_dir", "evidence_staging") is True


# --- RED test 11: partial-startup ownership --------------------------------


def test_partial_startup_central_db_created_before_node_db_failure_is_ledger_owned_at_that_moment(
    tmp_path, monkeypatch
):
    """Inject a failure between Central DB creation and Node DB
    creation; the ALREADY-created Central DB must already be
    ledger-owned AT THAT EXACT MOMENT -- never merely reconstructed
    afterward from a cleanup-exception's action name."""
    ledger = gate_runner.ResourceLedger()
    observed = {}

    real_create_database = e2e_support.create_database

    def _spy_create_database(db_admin, gate_names, member):
        if member == "node":
            # Central DB creation has already succeeded and returned by
            # this point -- check the ledger's live state BEFORE Node
            # DB creation is even attempted.
            observed["central_live_owned"] = ledger.contains("central_db", gate_names.central)
            raise RuntimeError("simulated Node DB creation failure")
        return real_create_database(db_admin, gate_names, member)

    monkeypatch.setattr(e2e_support, "create_database", _spy_create_database)

    with (
        pytest.raises(RuntimeError, match="simulated Node DB creation failure"),
        dual_deployment(tmp_path, start_node=False, resource_ledger=ledger) as _deployment,
    ):
        pytest.fail("must never yield")

    assert observed.get("central_live_owned") is True
    # After cleanup runs (real, unpatched drop), the ledger reflects
    # verified final disposal.
    assert ledger.is_empty()


# --- RED test 12: ownership correctness does not depend on cleanup ---------
# --- action names -----------------------------------------------------------


def test_ownership_correctness_does_not_depend_on_cleanup_action_names(tmp_path, monkeypatch):
    """Force workspace disposal specifically to fail (via a genuinely
    different underlying failure, not a name-matched one) while Central/
    Node database disposal succeeds normally. Release must be driven
    purely by each resource's OWN verified disposal outcome -- proven
    here because there is no cleanup-action-name-to-resource-kind table
    left in production code at all (Gap 2's fix deletes it entirely);
    workspace remains retained while Central/Node databases are
    correctly released, with nothing in this test ever depending on
    what any cleanup action happens to be named."""
    ledger = gate_runner.ResourceLedger()
    run_id = _run_id()
    gate_names = gate_runner.GateDatabaseNames.for_run(run_id)

    def _broken_remove_workspace(path):
        raise RuntimeError("simulated workspace removal failure -- not a KeyError/name-lookup issue")

    monkeypatch.setattr(e2e_support, "remove_workspace", _broken_remove_workspace)

    with (
        pytest.raises(CleanupAggregateError),
        dual_deployment(tmp_path, start_node=False, run_id=run_id, resource_ledger=ledger) as deployment,
    ):
        workspace_path = deployment.tmp_dir

    # Workspace disposal failed -- it must remain ledger-owned.
    assert ledger.contains("workspace", str(workspace_path)) is True
    # Central/Node database disposal succeeded independently -- both
    # must be released, proving per-resource independence rather than
    # an all-or-nothing name-matched table.
    assert ledger.contains("central_db", gate_names.central) is False
    assert ledger.contains("node_db", gate_names.node) is False

    # Real cleanup for the test's own deliberately-leaked workspace.
    import shutil

    shutil.rmtree(workspace_path, ignore_errors=True)
