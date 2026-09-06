"""Task 15 Task 11 corrective v10, Gap 3 (design spec Sections 12-14,
23, 28): `ResourceKind` carried no class distinction at all, and
`ResourceLedger.retained_execution_resources()` returned EVERY still-
owned resource regardless of kind. Since corrective v9 made `OUTER_ROOT`
and `STAGING_DIR` actively/live tracked, a run where ONLY the outer root
(or ONLY the evidence staging directory) failed to clean up -- nothing
execution-related retained at all -- made `execution_resources_retained`
come back `True`: semantically wrong, since nothing execution-related
was actually retained, only infrastructure/publication bookkeeping.

Every code path that can leave `OUTER_ROOT` or `STAGING_DIR` retained
ALSO unconditionally records a cleanup/finalization failure in the same
branch, so PASS is already blocked independently of this field --
narrowing `execution_resources_retained` to EXECUTION-class-only cannot
open a false-PASS path; these tests prove exactly that.

These are the mandatory RED tests from the v10 design spec, Section 25,
items 7-8.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import scripts.release_gate.evidence as evidence_module
from scripts.release_gate import gate_runner
from scripts.release_gate.environment import DatabaseAdminConnection, GateEnvironment

_OK_VALIDATION = {
    "clean_tree": True,
    "central_check": "ok",
    "node_check": "ok",
    "central_migration_drift": "none",
    "node_migration_drift": "none",
}


def _fake_run_validation(env):
    return dict(_OK_VALIDATION)


def _fake_environment_builder(*, repo_root, keep_on_failure=False):
    import os

    run_id = "rresclasstest"
    return GateEnvironment(
        run_id=run_id,
        git_sha="a" * 40,
        repo_root=Path(repo_root),
        artifact_dir=Path(repo_root) / "artifacts" / "phase1-release" / run_id,
        db_admin=DatabaseAdminConnection(
            host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            user=os.environ.get("POSTGRES_USER", "linuxlab"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
        ),
        keep_on_failure=keep_on_failure,
    )


def _trivial_scenario(deployment):
    return {}


@dataclass
class _FakeDeployment:
    node_secret: str = "sentinel"
    private_key_path: object = None
    tmp_dir: object = None
    resource_ledger: object = None


@contextmanager
def _fake_dual_deployment(tmp_dir, **kwargs):
    # A fake, fast dual_deployment() -- no real Postgres/Gunicorn --
    # exactly like scripts.tests.test_release_gate_gate_runner.py's own
    # `_make_factory()`. Never itself acquires anything into the shared
    # ledger, so OUTER_ROOT (acquired by `_run_release_gate()` itself)
    # is the ONLY resource this run ever tracks.
    yield _FakeDeployment(tmp_dir=Path(tmp_dir))


def _run(tmp_path, **overrides):
    kwargs = {
        "repo_root": tmp_path,
        "check_platform": False,
        "environment_builder": _fake_environment_builder,
        "dual_deployment_factory": _fake_dual_deployment,
        "scenario_specs": (("fake_ok", _trivial_scenario),),
        "run_validation": _fake_run_validation,
        "tmp_root": tmp_path,
    }
    kwargs.update(overrides)
    return gate_runner.run_release_gate(**kwargs)


# --- RED test 25.7: outer-root-only residue ---------------------------------


def test_outer_root_only_residue_does_not_set_execution_resources_retained(tmp_path, monkeypatch):
    """The scenario itself never touches the ledger at all -- the ONLY
    resource ever acquired in this run is `OUTER_ROOT` (acquired
    unconditionally by `_run_release_gate()` itself). Forcing its own
    `shutil.rmtree()` removal to fail leaves EXACTLY that one resource
    retained -- a pure infrastructure residue case."""
    monkeypatch.setattr(gate_runner, "assert_linux_platform", lambda: None)

    def _always_fails(path, *args, **kwargs):
        raise OSError("simulated outer-root cleanup failure")

    monkeypatch.setattr(gate_runner.shutil, "rmtree", _always_fails)

    result = _run(tmp_path)

    assert result.evidence is not None
    assert result.evidence.cleanup_status == "failed"
    assert result.evidence.cleanup_failure is not None
    assert result.evidence.retention["execution_resources_retained"] is False
    assert result.overall != "PASS"
    assert result.overall == "CLEANUP_FAILURE"
    assert result.exit_code == 5


# --- RED test 25.8: staging-only residue ------------------------------------


def test_staging_only_residue_does_not_set_execution_resources_retained(tmp_path, monkeypatch):
    """Force evidence publication to fail AND the subsequent staging-
    directory cleanup to ALSO fail (StagingCleanupError -- genuine
    residue left on disk), while leaving the run's own outer-root
    cleanup to succeed normally. Only the staging directory (PUBLICATION
    class) remains retained -- a pure publication residue case."""
    monkeypatch.setattr(gate_runner, "assert_linux_platform", lambda: None)

    def _always_fails_write(path, text):
        raise RuntimeError("simulated evidence write failure")

    monkeypatch.setattr(evidence_module, "_atomic_write_text", _always_fails_write)

    real_rmtree = gate_runner.shutil.rmtree

    def _selective_rmtree_failure(path, *args, **kwargs):
        # Only the evidence staging directory's own cleanup fails here
        # -- the run's own outer-root removal (a DIFFERENT path, also
        # calling shutil.rmtree -- the SAME underlying module object)
        # must still succeed normally, so this residue is genuinely
        # staging-only.
        if ".staging-" in str(path):
            raise OSError("simulated staging cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(gate_runner.shutil, "rmtree", _selective_rmtree_failure)

    result = _run(tmp_path)

    assert result.evidence is not None
    assert result.evidence.cleanup_failure is not None
    assert result.evidence.retention["execution_resources_retained"] is False
    assert result.overall != "PASS"
    assert result.overall == "CLEANUP_FAILURE"
    assert result.exit_code == 5


# --- unit-level confirmation: ResourceLedger classification itself ---------


def test_ledger_excludes_infrastructure_and_publication_kinds_from_execution_retention():
    ledger = gate_runner.ResourceLedger()
    ledger.acquire(gate_runner.ResourceKind.OUTER_ROOT, "/tmp/fake-outer-root")
    ledger.acquire(gate_runner.ResourceKind.STAGING_DIR, "evidence_staging")

    assert ledger.retained_execution_resources() == ()
    # Still fully tracked for audit/diagnostic purposes.
    all_refs = ledger.all_retained_resources()
    assert len(all_refs) == 2
    assert ledger.contains(gate_runner.ResourceKind.OUTER_ROOT, "/tmp/fake-outer-root") is True
    assert ledger.contains(gate_runner.ResourceKind.STAGING_DIR, "evidence_staging") is True


def test_ledger_includes_execution_class_kinds_in_execution_retention():
    ledger = gate_runner.ResourceLedger()
    ledger.acquire(gate_runner.ResourceKind.CENTRAL_DB, "fake_central_db")
    ledger.acquire(gate_runner.ResourceKind.OUTER_ROOT, "/tmp/fake-outer-root")

    retained = ledger.retained_execution_resources()
    assert len(retained) == 1
    assert retained[0].kind == gate_runner.ResourceKind.CENTRAL_DB
