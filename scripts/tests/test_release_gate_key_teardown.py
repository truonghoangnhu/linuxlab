"""Task 15 Tasks 7-10 corrective, Critical 1: proves the ephemeral
signing private key is deleted on every real dual_deployment() exit
path -- success, an assertion failure raised inside the `with` block,
and a partial setup failure that never reaches the yielded block at
all. Real ephemeral PostgreSQL databases and real Gunicorn processes
throughout, exactly like the rest of the Task 8-10 suite.
"""

import pytest

from scripts.release_gate import e2e_support
from scripts.release_gate.processes import StartupError


def test_private_key_absent_after_normal_dual_deployment_teardown(tmp_path):
    with e2e_support.dual_deployment(tmp_path) as deployment:
        private_key_path = deployment.private_key_path
        assert private_key_path.exists()
    assert not private_key_path.exists()


def test_private_key_absent_after_assertion_failure_inside_dual_deployment(tmp_path):
    captured = {}
    with pytest.raises(AssertionError), e2e_support.dual_deployment(tmp_path) as deployment:
        captured["path"] = deployment.private_key_path
        assert deployment.private_key_path.exists()
        raise AssertionError("simulated scenario assertion failure")
    assert not captured["path"].exists()


def test_private_key_absent_after_partial_setup_failure_before_yield(tmp_path, monkeypatch):
    """Forces wait_ready() to fail the moment Central's listener is up,
    well before dual_deployment() ever reaches its own `yield` -- proving
    cleanup for a setup failure that happens before there is any
    DualDeployment object to read `.private_key_path` from at all."""

    def _always_fail(managed_process, timeout_seconds, **kwargs):
        raise StartupError("forced failure for partial-setup-failure proof")

    monkeypatch.setattr(e2e_support, "wait_ready", _always_fail)

    with pytest.raises(StartupError), e2e_support.dual_deployment(tmp_path):
        pytest.fail("must never reach the yielded block")

    # The key lives inside a workspace subdirectory whose exact name is
    # not known here (setup failed before any DualDeployment existed to
    # expose it) -- rglob proves no such file leaked ANYWHERE under
    # tmp_path, a strictly stronger check than a single exact path.
    assert list(tmp_path.rglob("release-gate-signing.pem")) == []
