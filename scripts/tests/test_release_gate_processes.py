"""Task 15 Gunicorn process orchestration and readiness waiting
(T15-D12). Pure-Python: readiness polling is tested against a tiny
local stdlib HTTP server, not a real Gunicorn/Django process; process
lifecycle is tested against a real (but trivial) child subprocess.
"""

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts.release_gate.processes import (
    StartupError,
    reserve_local_port,
    stop_process,
    wait_ready,
)

# --- reserve_local_port ----------------------------------------------------


def test_reserve_local_port_returns_a_usable_port_number():
    port = reserve_local_port()

    assert isinstance(port, int)
    assert 1024 < port < 65536


def test_reserve_local_port_returns_different_ports_across_calls():
    ports = {reserve_local_port() for _ in range(5)}

    assert len(ports) == 5


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


# --- stop_process --------------------------------------------------------


def test_stop_process_terminates_a_running_child():
    from scripts.release_gate.processes import ManagedProcess

    popen = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    managed = ManagedProcess(popen=popen, port=0, log_path=None)

    stop_process(managed, graceful_seconds=2)

    assert popen.poll() is not None  # process has exited


def test_stop_process_is_a_safe_no_op_for_an_already_exited_child():
    from scripts.release_gate.processes import ManagedProcess

    popen = subprocess.Popen([sys.executable, "-c", "pass"])
    popen.wait()
    managed = ManagedProcess(popen=popen, port=0, log_path=None)

    stop_process(managed, graceful_seconds=2)  # does not raise


def test_stop_process_escalates_to_kill_if_graceful_termination_times_out():
    from scripts.release_gate.processes import ManagedProcess

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
