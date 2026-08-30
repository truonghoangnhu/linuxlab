"""Task 15 Gunicorn process orchestration and readiness waiting
(T15-D12). Release authority never uses `manage.py runserver`; it
starts real Gunicorn processes bound only to loopback dynamic ports
and waits on `/readyz`, never a fixed startup sleep.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class StartupError(Exception):
    """Raised when a managed process fails to become ready within its
    timeout. Maps to STARTUP_FAILURE (exit 3)."""


def reserve_local_port() -> int:
    """Ask the OS for a free loopback port, then release it
    immediately. There is an inherent (small, accepted) race between
    release and the caller actually binding it -- acceptable for a
    local/CI release-gate run, not a production allocator."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class ManagedProcess:
    popen: subprocess.Popen
    port: int
    log_path: Path | None


def start_gunicorn(profile_env: dict, port: int, log_path, *, repo_root=None, workers=1):
    """Start Gunicorn bound only to 127.0.0.1:<port>, serving
    linuxlab.wsgi:application under the given profile environment
    (typically at least DJANGO_SETTINGS_MODULE and the profile's own
    POSTGRES_*/database env). No secret is ever placed on argv -- the
    profile environment is passed through the child process
    environment only."""
    repo_root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    gunicorn_bin = str(Path(sys.executable).parent / "gunicorn")
    argv = [
        gunicorn_bin,
        "--bind", f"127.0.0.1:{port}",
        "--workers", str(workers),
        "--log-file", str(log_path),
        "--access-logfile", str(log_path),
        "--capture-output",
        "linuxlab.wsgi:application",
    ]
    child_env = {**os.environ, **profile_env}
    popen = subprocess.Popen(argv, cwd=str(repo_root), env=child_env)
    return ManagedProcess(popen=popen, port=port, log_path=log_path)


def wait_ready(base_url: str, timeout_seconds: float, *, poll_interval_seconds: float = 0.2) -> None:
    """Poll `<base_url>/readyz` until it returns 200 with
    {"status": "ok"}, or raise StartupError once `timeout_seconds` has
    elapsed. Never an unbounded/infinite retry loop."""
    deadline = time.monotonic() + timeout_seconds
    last_error = "no attempt made"
    url = base_url.rstrip("/") + "/readyz"

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=poll_interval_seconds) as response:
                if response.status == 200:
                    body = json.loads(response.read())
                    if body.get("status") == "ok":
                        return
                    last_error = f"not ready: {body}"
                else:
                    last_error = f"unexpected status {response.status}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(poll_interval_seconds)

    raise StartupError(f"{url} did not become ready within {timeout_seconds}s: {last_error}")


def stop_process(managed: ManagedProcess, graceful_seconds: float = 10.0) -> None:
    """Terminate exactly this managed child: SIGTERM, wait up to
    `graceful_seconds`, escalate to SIGKILL only if it hasn't exited by
    then. A safe no-op if the process has already exited."""
    popen = managed.popen
    if popen.poll() is not None:
        return
    popen.terminate()
    try:
        popen.wait(timeout=graceful_seconds)
    except subprocess.TimeoutExpired:
        popen.kill()
        popen.wait(timeout=graceful_seconds)
