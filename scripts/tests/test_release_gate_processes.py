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
race entirely.

Checkpoint corrective 2 (finding #2): the FD-inheritance fix closed the
bind race, but wait_ready() still accepted an INDEPENDENT `base_url`
argument alongside `managed_process` -- a caller (or an adversarial
one) could point the URL at a fake service while the ManagedProcess
happened to reference some unrelated live process, producing a false
PASS. wait_ready() now takes ONLY a `ManagedProcess` (no base_url
parameter at all): the readiness URL is derived exclusively from that
handle's own inherited listener identity (`managed_process.base_url`),
and the handle itself must be authentic -- produced by
start_gunicorn(), never hand-built by a caller -- so a forged handle
is rejected before any HTTP probe.
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


# --- test fixture helper: forging a ManagedProcess-shaped object -----------


def _forge_managed_process(*, popen, host="127.0.0.1", port, log_path=None, authentic=False):
    """Build a ManagedProcess-shaped object the way an adversarial or
    merely-careless caller might: bypass start_gunicorn() entirely via
    object.__new__()/object.__setattr__(). Used only to prove wait_ready()
    rejects anything it did not itself produce. `authentic=True` sets the
    real launcher token anyway, to isolate "was this produced by
    start_gunicorn()" from "does the listener identity actually match a
    live child" in tests that need a real ManagedProcess shape without
    going through a real Gunicorn subprocess."""
    import scripts.release_gate.processes as processes_module

    obj = object.__new__(ManagedProcess)
    object.__setattr__(obj, "popen", popen)
    object.__setattr__(obj, "listener_host", host)
    object.__setattr__(obj, "listener_port", port)
    object.__setattr__(obj, "log_path", log_path)
    token = processes_module._MANAGED_PROCESS_TOKEN if authentic else object()
    object.__setattr__(obj, "_launcher_token", token)
    return obj


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


def _live_child():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])


def test_wait_ready_retries_until_200_ok():
    server = _start_flaky_server(initial_failures=2)
    popen = _live_child()
    try:
        managed = _forge_managed_process(
            popen=popen, port=server.server_address[1], authentic=True
        )
        wait_ready(managed, timeout_seconds=5, poll_interval_seconds=0.05)  # does not raise
    finally:
        popen.terminate()
        popen.wait(timeout=5)
        server.shutdown()


def test_wait_ready_times_out_as_startup_error_not_infinite_loop():
    server = _start_flaky_server(initial_failures=10_000)
    popen = _live_child()
    try:
        managed = _forge_managed_process(
            popen=popen, port=server.server_address[1], authentic=True
        )
        with pytest.raises(StartupError):
            wait_ready(managed, timeout_seconds=0.3, poll_interval_seconds=0.05)
    finally:
        popen.terminate()
        popen.wait(timeout=5)
        server.shutdown()


def test_wait_ready_fails_closed_when_nothing_is_listening():
    popen = _live_child()
    try:
        managed = _forge_managed_process(popen=popen, port=1, authentic=True)
        with pytest.raises(StartupError):
            wait_ready(managed, timeout_seconds=0.3, poll_interval_seconds=0.05)
    finally:
        popen.terminate()
        popen.wait(timeout=5)


# --- adversarial: no independent base_url, no forged handle (finding #2) ---


def test_wait_ready_has_no_base_url_parameter():
    """The interface itself must not offer a way to mix an independent
    endpoint with a ManagedProcess handle."""
    import inspect

    params = inspect.signature(wait_ready).parameters
    assert "base_url" not in params


def test_wait_ready_never_false_passes_when_managed_child_has_already_exited():
    """A fake/unrelated service on some port always answers 200/ok --
    simulating a stale leftover process that has nothing to do with
    the release gate's own managed Gunicorn child. The child that was
    SUPPOSED to serve this port has already exited (failed to bind,
    crashed, whatever) -- readiness must never be satisfied by the
    fake service just because it happens to answer on that port."""
    server = _start_flaky_server(initial_failures=0)
    try:
        dead_popen = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_popen.wait()
        managed = _forge_managed_process(
            popen=dead_popen, port=server.server_address[1], authentic=True
        )

        with pytest.raises(StartupError):
            wait_ready(managed, timeout_seconds=1, poll_interval_seconds=0.05)
    finally:
        server.shutdown()


def test_wait_ready_never_false_passes_for_fake_service_plus_unrelated_live_child():
    """The exact checkpoint review scenario: a fake service that always
    answers ready, PLUS a genuinely live but completely unrelated child
    process, wired into a hand-built (non-authentic) handle whose
    listener identity points at the fake service. Before this
    corrective, ManagedProcess was a plain public dataclass any caller
    could construct this way; now the missing launcher token alone is
    enough to refuse it, regardless of the fake service being up and
    the child being alive."""
    server = _start_flaky_server(initial_failures=0)
    unrelated_live = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        managed = _forge_managed_process(
            popen=unrelated_live, port=server.server_address[1], authentic=False
        )

        with pytest.raises(StartupError):
            wait_ready(managed, timeout_seconds=1, poll_interval_seconds=0.05)
    finally:
        unrelated_live.terminate()
        unrelated_live.wait(timeout=5)
        server.shutdown()


def test_wait_ready_rejects_a_forged_handle_lacking_the_authentic_launcher_token():
    """Even a handle whose listener identity and child are entirely
    correct must be rejected if it was not actually produced by
    start_gunicorn() -- the token check is the first line of defense,
    independent of whether the rest of the handle 'looks right'."""
    server = _start_flaky_server(initial_failures=0)
    popen = _live_child()
    try:
        forged = _forge_managed_process(
            popen=popen, port=server.server_address[1], authentic=False
        )

        with pytest.raises(StartupError):
            wait_ready(forged, timeout_seconds=1, poll_interval_seconds=0.05)
    finally:
        popen.terminate()
        popen.wait(timeout=5)
        server.shutdown()


def test_wait_ready_rejects_a_plain_object_that_is_not_a_managed_process_at_all():
    with pytest.raises(StartupError):
        wait_ready("http://127.0.0.1:1", timeout_seconds=0.3, poll_interval_seconds=0.05)


def test_wait_ready_fails_the_moment_the_managed_child_exits_mid_wait():
    """The child starts alive (so an initial poll could plausibly see a
    real response) but exits before ever answering ready -- wait_ready
    must not keep polling an orphaned listener as if the child were
    still the one serving it."""
    server = _start_flaky_server(initial_failures=10_000)  # never actually becomes ready
    try:
        popen = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.1)"])
        managed = _forge_managed_process(
            popen=popen, port=server.server_address[1], authentic=True
        )

        with pytest.raises(StartupError):
            wait_ready(managed, timeout_seconds=5, poll_interval_seconds=0.05)
    finally:
        server.shutdown()


def test_wait_ready_still_succeeds_for_a_genuinely_alive_managed_child():
    server = _start_flaky_server(initial_failures=1)
    popen = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        managed = _forge_managed_process(
            popen=popen, port=server.server_address[1], authentic=True
        )

        wait_ready(managed, timeout_seconds=5, poll_interval_seconds=0.05)  # does not raise
    finally:
        popen.terminate()
        popen.wait(timeout=5)
        server.shutdown()


def test_wait_ready_rechecks_liveness_immediately_after_a_successful_probe():
    """The child answers ready, but exits in the instant between the HTTP
    response and wait_ready() accepting it. This must still fail --
    readiness is never granted for a child that is not confirmed alive
    at the moment of acceptance."""
    server = _start_flaky_server(initial_failures=0)
    popen = subprocess.Popen([sys.executable, "-c", "pass"])  # exits almost immediately
    popen.wait()
    try:
        managed = _forge_managed_process(
            popen=popen, port=server.server_address[1], authentic=True
        )

        with pytest.raises(StartupError):
            wait_ready(managed, timeout_seconds=1, poll_interval_seconds=0.05)
    finally:
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
        assert managed.base_url == f"http://127.0.0.1:{listener.port}"
        wait_ready(managed, timeout_seconds=15)

        import urllib.request

        with urllib.request.urlopen(f"{managed.base_url}/healthz", timeout=5) as response:
            assert response.status == 200
    finally:
        stop_process(managed, graceful_seconds=5)


def test_start_gunicorn_never_binds_0_0_0_0():
    listener = reserve_listener()
    try:
        assert listener.host == "127.0.0.1"
    finally:
        listener.sock.close()


def test_start_gunicorn_produces_an_authentic_handle_wait_ready_accepts():
    listener = reserve_listener()
    log_path_dir = os.environ.get("TMPDIR", "/tmp")
    log_path = f"{log_path_dir}/linuxlab-gate-authentic-handle.log"
    profile_env = {
        "DJANGO_SETTINGS_MODULE": "linuxlab.settings.node",
        "POSTGRES_DB": os.environ.get("POSTGRES_DB", "linuxlab_node_dev"),
    }

    managed = start_gunicorn(profile_env, listener, log_path)
    try:
        wait_ready(managed, timeout_seconds=15)  # does not raise: real, authentic handle
    finally:
        stop_process(managed, graceful_seconds=5)


# --- ManagedProcess construction boundary -----------------------------


def test_managed_process_has_no_public_keyword_constructor():
    with pytest.raises(TypeError):
        ManagedProcess(popen=None, listener_host="127.0.0.1", listener_port=1, log_path=None)


def test_managed_process_base_url_is_derived_not_writable():
    popen = _live_child()
    try:
        managed = _forge_managed_process(popen=popen, port=54321, authentic=True)
        assert managed.base_url == "http://127.0.0.1:54321"
        with pytest.raises((AttributeError, TypeError)):
            managed.listener_port = 1
    finally:
        popen.terminate()
        popen.wait(timeout=5)


# --- stop_process --------------------------------------------------------
#
# stop_process() carries no identity-trust decision -- it just signals
# whatever Popen it is handed -- so its tests use plain forged handles
# via the same test-only helper, without needing the authentic token.


def test_stop_process_terminates_a_running_child():
    popen = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    managed = _forge_managed_process(popen=popen, port=0)

    stop_process(managed, graceful_seconds=2)

    assert popen.poll() is not None  # process has exited


def test_stop_process_is_a_safe_no_op_for_an_already_exited_child():
    popen = subprocess.Popen([sys.executable, "-c", "pass"])
    popen.wait()
    managed = _forge_managed_process(popen=popen, port=0)

    stop_process(managed, graceful_seconds=2)  # does not raise


def test_stop_process_escalates_to_kill_if_graceful_termination_times_out():
    popen = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ]
    )
    managed = _forge_managed_process(popen=popen, port=0)
    start = time.monotonic()

    stop_process(managed, graceful_seconds=0.5)

    assert popen.poll() is not None
    assert time.monotonic() - start < 10  # escalated promptly, not left hanging
