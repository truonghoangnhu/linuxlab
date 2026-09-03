"""Task 15 Tasks 7-10 corrective, Important 3: failure-safe process
restart ownership.

Hard ordering: start_gunicorn() returns a handle -> the caller's own
deployment-state slot (e.g. `deployment.central`) is reassigned to that
handle IMMEDIATELY -> wait_ready() runs only after that -> the handle
is used. Never start -> wait_ready -> assign, because a readiness
failure in that forbidden order can leave a genuinely live replacement
process reachable from nowhere the caller's own teardown will ever
look -- an orphan.

restart_gunicorn() in scripts/release_gate/processes.py enforces this
ordering itself: it never returns (or raises) without having already
called the caller-supplied `on_replacement_started` callback the moment
the replacement's ManagedProcess handle exists, before wait_ready() is
invoked at all.
"""

import socket
import time

import pytest

from scripts.release_gate.e2e_support import dual_deployment
from scripts.release_gate.processes import StartupError, restart_gunicorn


def _process_is_gone(pid, *, deadline_seconds=5):
    import os

    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.1)
    return False


def _port_is_closed(host, port, *, deadline_seconds=5):
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                pass
        except OSError:
            return True
        time.sleep(0.1)
    return False


def _assert_restart_is_failure_safe(deployment, which):
    old_managed = getattr(deployment, which)
    assert old_managed.popen.poll() is None, "precondition: original process must be genuinely alive"
    old_pid = old_managed.popen.pid

    profile_env = getattr(deployment, f"{which}_profile_env")
    bad_profile_env = {**profile_env, "POSTGRES_DB": "linuxlab_gate_restart_ownership_missing_db"}

    registered = {}

    def _on_replacement_started(new_managed):
        registered["new"] = new_managed
        setattr(deployment, which, new_managed)

    with pytest.raises(StartupError):
        restart_gunicorn(
            old_managed,
            bad_profile_env,
            deployment.tmp_dir / f"{which}-restart-failure.log",
            timeout_seconds=3,
            on_replacement_started=_on_replacement_started,
        )

    assert "new" in registered, "on_replacement_started must be called even though readiness failed"
    new_managed = registered["new"]
    assert new_managed is not old_managed
    assert getattr(deployment, which) is new_managed, (
        "the caller's own deployment-state slot must already be reassigned "
        "to the replacement before wait_ready()'s failure ever propagates"
    )
    assert new_managed.popen.poll() is None, (
        "the replacement must still be genuinely alive -- this proves the "
        "failure came from wait_ready() timing out against a real but "
        "misconfigured process, never from the child itself dying"
    )
    return old_pid, new_managed.popen.pid, new_managed.listener_host, new_managed.listener_port


def test_restart_gunicorn_is_failure_safe_for_central(tmp_path):
    with dual_deployment(tmp_path, start_node=False) as deployment:
        old_pid, new_pid, new_host, new_port = _assert_restart_is_failure_safe(deployment, "central")

    # dual_deployment()'s own teardown stops whatever `deployment.central`
    # points to NOW -- the replacement, registered before the failure --
    # so both the original and the failed replacement must be gone, and
    # its port closed, with no orphan child left behind.
    assert _process_is_gone(old_pid)
    assert _process_is_gone(new_pid)
    assert _port_is_closed(new_host, new_port)


def test_restart_gunicorn_is_failure_safe_for_node(tmp_path):
    with dual_deployment(tmp_path) as deployment:
        old_pid, new_pid, new_host, new_port = _assert_restart_is_failure_safe(deployment, "node")

    assert _process_is_gone(old_pid)
    assert _process_is_gone(new_pid)
    assert _port_is_closed(new_host, new_port)


def test_restart_gunicorn_registers_replacement_before_wait_ready_even_on_success(tmp_path):
    """Same ordering proof on the SUCCESS path: on_replacement_started()
    must have already run by the time restart_gunicorn() returns (it
    could hardly return the handle otherwise), and the handle it
    receives must be the fully-ready one the caller then uses."""
    with dual_deployment(tmp_path, start_node=False) as deployment:
        old_central = deployment.central
        old_pid = old_central.popen.pid

        registered = {}

        def _on_replacement_started(new_managed):
            registered["called_before_return"] = True
            deployment.central = new_managed

        new_central = restart_gunicorn(
            old_central,
            deployment.central_profile_env,
            deployment.tmp_dir / "central-restart-success.log",
            timeout_seconds=20,
            on_replacement_started=_on_replacement_started,
        )

        assert registered.get("called_before_return") is True
        assert deployment.central is new_central
        assert _process_is_gone(old_pid)

        # The replacement is genuinely ready and usable.
        import urllib.request

        with urllib.request.urlopen(new_central.base_url.rstrip("/") + "/readyz", timeout=5) as response:
            assert response.status == 200
