"""Task 15 Gunicorn process orchestration and readiness waiting
(T15-D12). Release authority never uses `manage.py runserver`.

Checkpoint corrective (finding #2): a URL alone is not proof of
identity. The parent reserves a real bound+listening loopback socket
(reserve_listener()) and keeps it open continuously -- Gunicorn
inherits that EXACT file descriptor (`--bind fd://<fd>`, `pass_fds`)
rather than being told a bare port number to rebind by itself, so
there is no window in which an unrelated process could occupy that
port between reservation and Gunicorn's own bind.

Checkpoint corrective 2 (finding #2): FD inheritance alone was not
enough -- wait_ready() still took an INDEPENDENT `base_url` argument
alongside `managed_process`, so a fake service could be probed while an
unrelated live child satisfied the liveness check, producing a false
PASS. wait_ready() now takes ONLY a ManagedProcess: the readiness URL
is derived exclusively from that handle's own inherited listener
(`managed_process.base_url`), and the handle itself must be authentic
-- produced by start_gunicorn(), the only supported producer, never
hand-built by a caller. Liveness is re-checked immediately before every
probe AND immediately after a successful one, closing the child-exit
race on both sides of the HTTP round trip.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


class StartupError(Exception):
    """Raised when a managed process fails to become ready within its
    timeout, has already exited, or is not an authentic
    start_gunicorn()-produced handle. Maps to STARTUP_FAILURE (exit 3)."""


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


# Identifies a ManagedProcess as genuinely produced by start_gunicorn()
# in THIS process. Defense in depth only -- the real safety property is
# that wait_ready() derives its probe URL exclusively from the handle's
# own listener_host/listener_port, never from independent caller input,
# so there is no longer any way to point the probe at one service while
# checking liveness against an unrelated process.
_MANAGED_PROCESS_TOKEN = object()


@dataclass(frozen=True, init=False)
class ManagedProcess:
    """An authentic handle to a Gunicorn child bound to a specific
    inherited listener. The ONLY supported producer is start_gunicorn()
    -- there is no public keyword constructor -- so a caller can never
    hand wait_ready() a plausible-looking handle that does not actually
    correspond to a process this module started."""

    popen: subprocess.Popen
    listener_host: str
    listener_port: int
    log_path: Path | None
    _launcher_token: object = field(repr=False, compare=False)

    @classmethod
    def _produced_by_start_gunicorn(cls, *, popen, listener_host, listener_port, log_path):
        obj = object.__new__(cls)
        object.__setattr__(obj, "popen", popen)
        object.__setattr__(obj, "listener_host", listener_host)
        object.__setattr__(obj, "listener_port", listener_port)
        object.__setattr__(obj, "log_path", log_path)
        object.__setattr__(obj, "_launcher_token", _MANAGED_PROCESS_TOKEN)
        return obj

    @property
    def base_url(self) -> str:
        return f"http://{self.listener_host}:{self.listener_port}"


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
    host, port = listener.host, listener.port
    listener.sock.close()
    return ManagedProcess._produced_by_start_gunicorn(
        popen=popen, listener_host=host, listener_port=port, log_path=log_path
    )


def _require_authentic_managed_process(managed_process) -> None:
    if (
        not isinstance(managed_process, ManagedProcess)
        or getattr(managed_process, "_launcher_token", None) is not _MANAGED_PROCESS_TOKEN
    ):
        raise StartupError(
            "wait_ready() requires an authentic ManagedProcess produced by "
            "start_gunicorn() -- a hand-built or forged handle is never accepted"
        )


def wait_ready(
    managed_process: ManagedProcess,
    timeout_seconds: float,
    *,
    poll_interval_seconds: float = 0.2,
) -> None:
    """Poll `managed_process.base_url + "/readyz"` until it returns 200
    with {"status": "ok"}, or raise StartupError once `timeout_seconds`
    has elapsed. There is deliberately no independent `base_url`
    parameter: the probe target is derived exclusively from the
    authentic handle's own inherited listener, so a caller can never
    probe one endpoint while `managed_process` liveness-checks an
    unrelated process. Liveness is checked immediately before every
    probe AND immediately after a successful one -- a child that exits
    in either window never yields a false readiness PASS."""
    _require_authentic_managed_process(managed_process)
    deadline = time.monotonic() + timeout_seconds
    last_error = "no attempt made"
    url = managed_process.base_url.rstrip("/") + "/readyz"

    while time.monotonic() < deadline:
        _require_authentic_managed_process(managed_process)
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
                        exit_code = managed_process.popen.poll()
                        if exit_code is not None:
                            raise StartupError(
                                f"managed process for {url} exited (code {exit_code}) "
                                "immediately after answering ready -- refusing to "
                                "grant readiness for a child no longer confirmed alive"
                            )
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
