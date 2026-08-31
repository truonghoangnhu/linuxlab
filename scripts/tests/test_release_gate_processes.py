"""Task 15 Gunicorn process orchestration and readiness waiting
(T15-D12).

Checkpoint corrective (finding #2): a URL alone is not proof of
identity. The old design reserved a port by binding then immediately
releasing it, handing only the bare port NUMBER to Gunicorn -- a
window existed between release and Gunicorn's own bind() in which an
unrelated process could occupy that exact port, and wait_ready() (URL
polling only) had no way to detect that the thing answering 200 wasn't
actually the managed child.

The parent now reserves a real bound+listening socket
(reserve_listener()) and keeps it open continuously; Gunicorn inherits
that exact file descriptor (`--bind fd://<fd>`, `pass_fds`) rather than
being told a bare port number to rebind by itself, closing the handoff
race entirely. wait_ready() additionally takes the ManagedProcess
handle and fails immediately if that exact child has already exited,
so a healthy-looking response from an unrelated service can never
satisfy readiness for a child that never became (or stopped being)
that listener's owner.
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts.release_gate.processes import (
    ManagedProcess,
    ReservedListener,
    StartupError,
    reserve_listener,
    reserve_local_port,
    start_gunicorn,
    stop_process,
    wait_ready,
)

# --- reserve_local_port (still a general-purpose utility) -------------------


def test_reserve_local_port_returns_a_usable_port_number():
    port = reserve_local_port()

    assert isinstance(port, int)
    assert 1024 < port < 65536


def test_reserve_local_port_returns_different_ports_across_calls():
    ports = {reserve_local_port() for _ in range(5)}

    assert len(ports) == 5


# --- reserve_listener: the socket stays open, owned by the caller ----------


def test_reserve_listener_returns_a_bound_open_socket():
    listener = reserve_listener()
    try:
        assert isinstance(listener, ReservedListener)
        assert listener.host == "127.0.0.1"
        assert 1024 < listener.port < 65536
        # The socket is genuinely bound and listening RIGHT NOW -- a
        # second bind attempt on the exact same port must fail while
        # this listener still owns it.
        import socket

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(OSError):
            probe.bind(("127.0.0.1", listener.port))
        probe.close()
    finally:
        listener.sock.close()


def test_reserve_listener_returns_different_ports_across_calls():
    a = reserve_listener()
    b = reserve_listener()
    try:
        assert a.port != b.port
    finally:
        a.sock.close()
        b.sock.close()


# --- wait_ready --------------------------------------------------------


class _FlakyReadyHandler(BaseHTTPRequestHandler):
    _lock = threading.Lock()
    _remaining_failures = 0

    def do_GET(self):
        with _FlakyReadyHandler._lock:
            if _FlakyReadyHandler._remaining_failures > 0:
                _FlakyReadyHandler._remaining_failures -= 1
                self.send_response(503)
                self.end_headers()
                self.wfile.write(json.dumps({"status": "not_ready"}).encode())
                return
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _start_flaky_server(initial_failures):
    _FlakyReadyHandler._remaining_failures = initial_failures
    server = HTTPServer(("127.0.0.1", 0), _FlakyReadyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_wait_ready_retries_until_200_ok():
    server = _start_flaky_server(initial_failures=2)
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        wait_ready(base_url, timeout_seconds=5, poll_interval_seconds=0.05)  # does not raise
    finally:
        server.shutdown()


def test_wait_ready_times_out_as_startup_error_not_infinite_loop():
    server = _start_flaky_server(initial_failures=10_000)
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(StartupError):
            wait_ready(base_url, timeout_seconds=0.3, poll_interval_seconds=0.05)
    finally:
        server.shutdown()


def test_wait_ready_fails_closed_when_nothing_is_listening():
    with pytest.raises(StartupError):
        wait_ready("http://127.0.0.1:1", timeout_seconds=0.3, poll_interval_seconds=0.05)


# --- adversarial: wrong-service identity (checkpoint finding #2) -----------


def test_wait_ready_never_false_passes_when_managed_child_has_already_exited():
    """A fake/unrelated service on some port always answers 200/ok --
    simulating a stale leftover process that has nothing to do with
    the release gate's own managed Gunicorn child. The child that was
    SUPPOSED to serve this port has already exited (failed to bind,
    crashed, whatever) -- readiness must never be satisfied by the
    fake service just because it happens to answer on that port."""
    server = _start_flaky_server(initial_failures=0)
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"

        dead_popen = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_popen.wait()
        managed = ManagedProcess(popen=dead_popen, port=server.server_address[1], log_path=None)

        with pytest.raises(StartupError):
            wait_ready(
                base_url, timeout_seconds=1, poll_interval_seconds=0.05, managed_process=managed
            )
    finally:
        server.shutdown()


def test_wait_ready_fails_the_moment_the_managed_child_exits_mid_wait():
    """The child starts alive (so an initial poll could plausibly see a
    real response) but exits before ever answering ready -- wait_ready
    must not keep polling an orphaned listener as if the child were
    still the one serving it."""
    server = _start_flaky_server(initial_failures=10_000)  # never actually becomes ready
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        popen = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.1)"])
        managed = ManagedProcess(popen=popen, port=server.server_address[1], log_path=None)

        with pytest.raises(StartupError):
            wait_ready(
                base_url, timeout_seconds=5, poll_interval_seconds=0.05, managed_process=managed
            )
    finally:
        server.shutdown()


def test_wait_ready_still_succeeds_for_a_genuinely_alive_managed_child():
    server = _start_flaky_server(initial_failures=1)
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        popen = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        managed = ManagedProcess(popen=popen, port=server.server_address[1], log_path=None)

        wait_ready(
            base_url, timeout_seconds=5, poll_interval_seconds=0.05, managed_process=managed
        )  # does not raise
    finally:
        popen.terminate()
        popen.wait(timeout=5)
        server.shutdown()


# --- start_gunicorn: FD-inherited listener, no bind handoff race -----------


def test_start_gunicorn_binds_the_exact_reserved_listener(tmp_path):
    """Real Gunicorn, real inherited-FD bind, real HTTP round trip --
    proves the managed child actually owns the exact listener the
    parent reserved, not merely 'a' Gunicorn on 'some' port."""
    listener = reserve_listener()
    log_path = tmp_path / "central.log"
    profile_env = {
        "DJANGO_SETTINGS_MODULE": "linuxlab.settings.central",
        "POSTGRES_DB": os.environ.get("POSTGRES_DB", "linuxlab_central_dev"),
    }

    managed = start_gunicorn(profile_env, listener, log_path)
    try:
        base_url = f"http://127.0.0.1:{listener.port}"
        wait_ready(base_url, timeout_seconds=15, managed_process=managed)

        import urllib.request

        with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
            assert response.status == 200
    finally:
        stop_process(managed, graceful_seconds=5)


def test_start_gunicorn_never_binds_0_0_0_0():
    listener = reserve_listener()
    try:
        assert listener.host == "127.0.0.1"
    finally:
        listener.sock.close()


# --- stop_process --------------------------------------------------------


def test_stop_process_terminates_a_running_child():
    popen = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    managed = ManagedProcess(popen=popen, port=0, log_path=None)

    stop_process(managed, graceful_seconds=2)

    assert popen.poll() is not None  # process has exited


def test_stop_process_is_a_safe_no_op_for_an_already_exited_child():
    popen = subprocess.Popen([sys.executable, "-c", "pass"])
    popen.wait()
    managed = ManagedProcess(popen=popen, port=0, log_path=None)

    stop_process(managed, graceful_seconds=2)  # does not raise


def test_stop_process_escalates_to_kill_if_graceful_termination_times_out():
    popen = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ]
    )
    managed = ManagedProcess(popen=popen, port=0, log_path=None)
    start = time.monotonic()

    stop_process(managed, graceful_seconds=0.5)

    assert popen.poll() is not None
    assert time.monotonic() - start < 10  # escalated promptly, not left hanging
