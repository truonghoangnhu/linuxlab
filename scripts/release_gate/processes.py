"""Task 15 Gunicorn process orchestration and readiness waiting
(T15-D12). Release authority never uses `manage.py runserver`.

Checkpoint corrective (finding #2): a URL alone is not proof of
identity. The parent reserves a real bound+listening loopback socket
(reserve_listener()) and keeps it open continuously -- Gunicorn
inherits that EXACT file descriptor (`--bind fd://<fd>`, `pass_fds`)
rather than being told a bare port number to rebind by itself, so
there is no window in which an unrelated process could occupy that
port between reservation and Gunicorn's own bind. wait_ready()
additionally takes the ManagedProcess handle and fails immediately if
that exact child has already exited, so a healthy-looking response
from an unrelated service can never satisfy readiness for a child that
never became (or stopped being) that listener's owner.
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
    timeout, or has already exited. Maps to STARTUP_FAILURE (exit 3)."""


def reserve_local_port() -> int:
    """General-purpose free-port allocator for callers that do not need
    continuous socket ownership (e.g. a test's own throwaway HTTP
    server). NOT used by start_gunicorn() -- see reserve_listener()."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class ReservedListener:
    """A real bound+listening socket the caller keeps open until a
    child process inherits its exact file descriptor. Unlike a bare
    port number, this cannot be silently reused by an unrelated
    process while release-gate orchestration is still setting up."""

    sock: socket.socket
    host: str
    port: int


def reserve_listener(host: str = "127.0.0.1") -> ReservedListener:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    return ReservedListener(sock=sock, host=host, port=port)


@dataclass
class ManagedProcess:
    popen: subprocess.Popen
    port: int
    log_path: Path | None


def start_gunicorn(profile_env: dict, listener: ReservedListener, log_path, *, repo_root=None, workers=1):
    """Start Gunicorn bound to the EXACT reserved listener via
    inherited file descriptor (`fd://<fd>`), serving
    linuxlab.wsgi:application under the given profile environment. No
    secret is ever placed on argv -- the profile environment is passed
    through the child process environment only. The parent's own copy
    of the socket is closed once the child has inherited it via
    `pass_fds`; from that point the child (and only the child) owns
    the open listening socket."""
    repo_root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fd = listener.sock.fileno()
    gunicorn_bin = str(Path(sys.executable).parent / "gunicorn")
    argv = [
        gunicorn_bin,
        "--bind", f"fd://{fd}",
        "--workers", str(workers),
        "--log-file", str(log_path),
        "--access-logfile", str(log_path),
        "--capture-output",
        "linuxlab.wsgi:application",
    ]
    child_env = {**os.environ, **profile_env}
    popen = subprocess.Popen(argv, cwd=str(repo_root), env=child_env, pass_fds=(fd,))
    listener.sock.close()
    return ManagedProcess(popen=popen, port=listener.port, log_path=log_path)


def wait_ready(
    base_url: str,
    timeout_seconds: float,
    *,
    poll_interval_seconds: float = 0.2,
    managed_process: ManagedProcess | None = None,
) -> None:
    """Poll `<base_url>/readyz` until it returns 200 with
    {"status": "ok"}, or raise StartupError once `timeout_seconds` has
    elapsed. If `managed_process` is given, fail immediately (never
    wait out the timeout) the moment that EXACT child has exited --
    a healthy-looking response from an unrelated service on the same
    port can never be mistaken for that child's own readiness."""
    deadline = time.monotonic() + timeout_seconds
    last_error = "no attempt made"
    url = base_url.rstrip("/") + "/readyz"

    while time.monotonic() < deadline:
        if managed_process is not None:
            exit_code = managed_process.popen.poll()
            if exit_code is not None:
                raise StartupError(
                    f"managed process for {url} exited (code {exit_code}) before "
                    "becoming ready -- refusing to trust any response from this "
                    "port, since it can no longer be this process's own listener"
                )
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
