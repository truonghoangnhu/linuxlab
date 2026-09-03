"""Task 15 Tasks 7-10 final cleanup corrective: every release artifact
dual_deployment() generates (private/public signing keys, .llpkg
files, pg_dump files, Gunicorn logs, other scenario temp artifacts)
lives inside ONE deployment-owned workspace directory, and that
workspace is entirely absent once the deployment exits -- on success,
on an exception raised inside the `with` block, and on a partial setup
failure that never reaches `yield` at all. Real ephemeral PostgreSQL
databases and real Gunicorn processes throughout, exactly like the
rest of the Task 8-10 suite. Repo fixtures and the pytest `tmp_path`
fixture itself are never deleted -- only the workspace subdirectory
dual_deployment() itself creates.
"""

import pytest

from scripts.release_gate import e2e_support
from scripts.release_gate.database import dump_database
from scripts.release_gate.processes import StartupError


def test_workspace_is_absent_after_normal_dual_deployment_exit(tmp_path):
    with e2e_support.dual_deployment(tmp_path, start_node=False) as deployment:
        workspace = deployment.tmp_dir
        assert workspace.exists()
        assert workspace.is_relative_to(tmp_path)
        assert deployment.private_key_path.exists()
        assert deployment.public_key_path.exists()
        assert deployment.private_key_path.is_relative_to(workspace)
        assert deployment.public_key_path.is_relative_to(workspace)
    assert not workspace.exists()
    # The pytest-owned parent directory itself must survive.
    assert tmp_path.exists()


def test_workspace_is_absent_after_an_exception_inside_dual_deployment(tmp_path):
    captured = {}
    with pytest.raises(AssertionError), e2e_support.dual_deployment(tmp_path, start_node=False) as deployment:
        captured["workspace"] = deployment.tmp_dir
        assert deployment.tmp_dir.exists()
        raise AssertionError("simulated scenario assertion failure")
    assert not captured["workspace"].exists()
    assert tmp_path.exists()


def test_workspace_is_absent_after_partial_setup_failure_before_yield(tmp_path, monkeypatch):
    """Forces wait_ready() to fail before dual_deployment() ever reaches
    its own `yield` -- there is no DualDeployment object to read
    `.tmp_dir` from at all, so this proves cleanup by scanning for any
    `gate-workspace-*` directory left under tmp_path."""

    def _always_fail(managed_process, timeout_seconds, **kwargs):
        raise StartupError("forced failure for partial-setup-failure proof")

    monkeypatch.setattr(e2e_support, "wait_ready", _always_fail)

    with pytest.raises(StartupError), e2e_support.dual_deployment(tmp_path, start_node=False):
        pytest.fail("must never reach the yielded block")

    leftover_workspaces = list(tmp_path.glob("gate-workspace-*"))
    assert leftover_workspaces == []
    assert tmp_path.exists()


def test_workspace_is_absent_after_a_backup_restore_style_failure(tmp_path):
    """A real pg_dump artifact is created inside the workspace, then the
    scenario body fails -- proving dump/package artifacts are cleaned up
    the same as every other workspace-owned file, not just the signing
    keys."""
    captured = {}
    with pytest.raises(RuntimeError), e2e_support.dual_deployment(tmp_path, start_node=False) as deployment:
        captured["workspace"] = deployment.tmp_dir
        dump_path = deployment.tmp_dir / "central.dump"
        dump_database(deployment.db_admin, deployment.gate_names, "central", dump_path)
        assert dump_path.exists()
        assert dump_path.stat().st_size > 0
        raise RuntimeError("simulated backup/restore scenario failure")
    assert not captured["workspace"].exists()
    assert tmp_path.exists()


def test_generated_package_and_logs_are_inside_the_workspace(tmp_path):
    """author_and_package()/dual_deployment() itself must place .llpkg
    files and Gunicorn logs under `deployment.tmp_dir` -- proven here by
    asserting every artifact discovered is a workspace descendant, and
    that they are all gone once the deployment exits."""
    with e2e_support.dual_deployment(tmp_path) as deployment:
        workspace = deployment.tmp_dir
        authored, package_path = e2e_support.author_and_package(
            deployment,
            prompt="Which command lists files?",
            options=[{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
            correct_option_id="a",
        )
        assert authored is not None
        assert package_path.is_relative_to(workspace)
        assert package_path.exists()

        central_log = deployment.central.log_path
        node_log = deployment.node.log_path
        assert central_log is not None and central_log.is_relative_to(workspace)
        assert node_log is not None and node_log.is_relative_to(workspace)

    assert not workspace.exists()
