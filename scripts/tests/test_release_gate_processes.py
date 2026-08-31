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
import socket
import subprocess
import sys
import threading
import time
import typing
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


# --- checkpoint corrective 3 (finding #2): socket is the sole source of truth


def test_reserved_listener_host_and_port_have_no_settable_fields():
    """host/port are properties computed from the live socket, not
    stored fields -- there is no attribute to redirect at all."""
    listener = reserve_listener()
    try:
        with pytest.raises(AttributeError):
            listener.port = 9999
        with pytest.raises(AttributeError):
            listener.host = "10.0.0.1"
    finally:
        listener.sock.close()


def test_reserved_listener_port_always_reflects_the_actual_socket():
    listener = reserve_listener()
    try:
        assert listener.port == listener.sock.getsockname()[1]
        assert listener.host == listener.sock.getsockname()[0]
    finally:
        listener.sock.close()


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
    expected_port = listener.port  # read BEFORE start_gunicorn() closes the socket
    log_path = tmp_path / "central.log"
    profile_env = {
        "DJANGO_SETTINGS_MODULE": "linuxlab.settings.central",
        "POSTGRES_DB": os.environ.get("POSTGRES_DB", "linuxlab_central_dev"),
    }

    managed = start_gunicorn(profile_env, listener, log_path)
    try:
        assert managed.base_url == f"http://127.0.0.1:{expected_port}"
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


def test_start_gunicorn_endpoint_always_matches_the_actual_inherited_socket(tmp_path):
    """The checkpoint review's exact scenario: real socket A, a fake
    ready service B, and an attempt to redirect the listener metadata
    to B. Since host/port are now computed from the live socket
    (`getsockname()`), read fresh immediately before launch, there is
    no field left to redirect at all -- the managed endpoint can only
    ever be A, and the fake B can never satisfy wait_ready()."""
    listener = reserve_listener()
    real_port = listener.port  # captured from the actual socket (A)

    fake_ready = _start_flaky_server(initial_failures=0)  # B: always answers ready
    try:
        # The redirect attempt itself is refused -- there is no
        # settable field to point at B in the first place.
        with pytest.raises(AttributeError):
            listener.port = fake_ready.server_address[1]

        log_path = tmp_path / "redirect-probe.log"
        profile_env = {
            "DJANGO_SETTINGS_MODULE": "linuxlab.settings.central",
            "POSTGRES_DB": os.environ.get("POSTGRES_DB", "linuxlab_central_dev"),
        }
        managed = start_gunicorn(profile_env, listener, log_path)
        try:
            assert managed.listener_port == real_port
            assert managed.base_url == f"http://127.0.0.1:{real_port}"
            assert managed.base_url != f"http://127.0.0.1:{fake_ready.server_address[1]}"

            # Readiness against the REAL Gunicorn child at A succeeds --
            # it was never possible to point it at B.
            wait_ready(managed, timeout_seconds=15)
        finally:
            stop_process(managed, graceful_seconds=5)
    finally:
        fake_ready.shutdown()


# --- checkpoint corrective 4 (finding #2): one trusted base-socket descriptor
#
# A socket SUBCLASS can retain the real underlying OS descriptor for
# listener A while overriding getsockname() (and/or fileno()) to report a
# completely different, genuinely live endpoint B. Before this
# corrective, start_gunicorn() called `listener.sock.fileno()` and
# `listener.sock.getsockname()` directly -- both overridable -- so
# Gunicorn could inherit A while the ManagedProcess identity (and every
# wait_ready() probe) pointed at B. The fix bypasses any caller-defined
# override entirely: it reads the true descriptor via the unbound
# `socket.socket.fileno`/`socket.socket.getsockname` base implementations,
# duplicates that descriptor into a plain base `socket.socket` the caller
# never had a chance to subclass, and uses ONLY that trusted duplicate for
# both the Gunicorn handoff and the ManagedProcess identity.


class _LyingGetsocknameSocket(socket.socket):
    """Real OS descriptor is a duplicate of listener A; getsockname()
    is overridden to report a fake endpoint B."""

    def __init__(self, dup_fd, fake_host, fake_port):
        super().__init__(fileno=dup_fd)
        self._fake_host = fake_host
        self._fake_port = fake_port

    def getsockname(self):
        return (self._fake_host, self._fake_port)


class _LyingFilenoSocket(socket.socket):
    """Real OS descriptor is a duplicate of listener A; fileno() is
    overridden to report an unrelated decoy descriptor."""

    def __init__(self, dup_fd, fake_fd):
        super().__init__(fileno=dup_fd)
        self._fake_fd = fake_fd

    def fileno(self):
        return self._fake_fd


class _LyingBothSocket(socket.socket):
    """Both fileno() and getsockname() are overridden inconsistently
    with the real underlying descriptor."""

    def __init__(self, dup_fd, fake_fd, fake_host, fake_port):
        super().__init__(fileno=dup_fd)
        self._fake_fd = fake_fd
        self._fake_host = fake_host
        self._fake_port = fake_port

    def fileno(self):
        return self._fake_fd

    def getsockname(self):
        return (self._fake_host, self._fake_port)


class _RecordingFakePopen:
    """Stands in for subprocess.Popen so overridden-fileno tests can
    assert exactly which descriptor was about to be handed to Gunicorn
    without actually spawning a real process bound to a decoy fd."""

    _instances: typing.ClassVar[list] = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.pass_fds = kwargs.get("pass_fds")
        _RecordingFakePopen._instances.append(self)

    def poll(self):
        return None


def test_start_gunicorn_endpoint_survives_a_lying_getsockname_subclass_and_real_gunicorn(
    tmp_path,
):
    """The exact remaining checkpoint-4 attack, proven against a REAL
    Gunicorn child: the caller-supplied socket subclass retains the
    genuine descriptor for listener A but overrides getsockname() to
    report a completely different, genuinely live, always-ready fake
    service B. The managed endpoint and the socket Gunicorn actually
    binds must both remain A; B must never be able to satisfy
    wait_ready()."""
    real_listener = reserve_listener()
    expected_port = real_listener.port
    fake_ready = _start_flaky_server(initial_failures=0)  # B: always answers 200/ok
    try:
        dup_fd = os.dup(socket.socket.fileno(real_listener.sock))
        lying_sock = _LyingGetsocknameSocket(
            dup_fd, "127.0.0.1", fake_ready.server_address[1]
        )
        wrapped = ReservedListener(sock=lying_sock)

        log_path = tmp_path / "lying-getsockname.log"
        profile_env = {
            "DJANGO_SETTINGS_MODULE": "linuxlab.settings.central",
            "POSTGRES_DB": os.environ.get("POSTGRES_DB", "linuxlab_central_dev"),
        }

        managed = start_gunicorn(profile_env, wrapped, log_path)
        try:
            assert managed.listener_port == expected_port
            assert managed.base_url == f"http://127.0.0.1:{expected_port}"
            assert managed.base_url != f"http://127.0.0.1:{fake_ready.server_address[1]}"

            wait_ready(managed, timeout_seconds=15)  # succeeds against the REAL child at A

            import urllib.request

            with urllib.request.urlopen(f"{managed.base_url}/healthz", timeout=5) as response:
                assert response.status == 200
        finally:
            stop_process(managed, graceful_seconds=5)
    finally:
        real_listener.sock.close()
        fake_ready.shutdown()


def test_start_gunicorn_bypasses_overridden_fileno_and_uses_true_descriptor(
    tmp_path, monkeypatch
):
    """A subclass whose fileno() lies about a decoy descriptor must
    never get that decoy passed to Gunicorn -- the true underlying
    descriptor (obtained via the base-class implementation) is what
    gets inherited, always."""
    real_listener = reserve_listener()
    expected_port = real_listener.port
    decoy_fd = os.open(os.devnull, os.O_RDONLY)
    try:
        dup_fd = os.dup(socket.socket.fileno(real_listener.sock))
        lying_sock = _LyingFilenoSocket(dup_fd, decoy_fd)
        wrapped = ReservedListener(sock=lying_sock)

        _RecordingFakePopen._instances.clear()
        monkeypatch.setattr(
            "scripts.release_gate.processes.subprocess.Popen", _RecordingFakePopen
        )
        profile_env = {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}

        managed = start_gunicorn(profile_env, wrapped, tmp_path / "fileno-lie.log")

        assert len(_RecordingFakePopen._instances) == 1
        captured = _RecordingFakePopen._instances[0]
        assert decoy_fd not in captured.pass_fds
        assert f"fd://{decoy_fd}" not in captured.argv
        assert managed.listener_port == expected_port
    finally:
        os.close(decoy_fd)
        real_listener.sock.close()


def test_start_gunicorn_bypasses_both_overridden_fileno_and_getsockname(tmp_path, monkeypatch):
    """Both overrides at once, inconsistently: fileno() points at a
    decoy descriptor, getsockname() points at a fake live ready
    service. Neither lie may reach Gunicorn or ManagedProcess."""
    real_listener = reserve_listener()
    expected_port = real_listener.port
    fake_ready = _start_flaky_server(initial_failures=0)
    decoy_fd = os.open(os.devnull, os.O_RDONLY)
    try:
        dup_fd = os.dup(socket.socket.fileno(real_listener.sock))
        lying_sock = _LyingBothSocket(
            dup_fd, decoy_fd, "127.0.0.1", fake_ready.server_address[1]
        )
        wrapped = ReservedListener(sock=lying_sock)

        _RecordingFakePopen._instances.clear()
        monkeypatch.setattr(
            "scripts.release_gate.processes.subprocess.Popen", _RecordingFakePopen
        )
        profile_env = {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}

        managed = start_gunicorn(profile_env, wrapped, tmp_path / "both-lie.log")

        assert len(_RecordingFakePopen._instances) == 1
        captured = _RecordingFakePopen._instances[0]
        assert decoy_fd not in captured.pass_fds
        assert f"fd://{decoy_fd}" not in captured.argv
        assert managed.listener_port == expected_port
        assert managed.listener_port != fake_ready.server_address[1]
        assert managed.base_url != f"http://127.0.0.1:{fake_ready.server_address[1]}"
    finally:
        os.close(decoy_fd)
        real_listener.sock.close()
        fake_ready.shutdown()


def test_start_gunicorn_refuses_a_lying_subclass_whose_true_descriptor_is_closed(
    tmp_path, monkeypatch
):
    """The overridden getsockname() still reports a plausible-looking
    live endpoint, but the TRUE underlying descriptor is closed -- this
    must be rejected before Gunicorn is ever spawned."""
    real_listener = reserve_listener()
    try:
        dup_fd = os.dup(socket.socket.fileno(real_listener.sock))
        lying_sock = _LyingGetsocknameSocket(dup_fd, "127.0.0.1", 1)
        os.close(dup_fd)  # true descriptor now closed; override keeps lying

        wrapped = ReservedListener(sock=lying_sock)

        def _fail_if_spawned(*args, **kwargs):
            raise AssertionError(
                "must not spawn Gunicorn against a closed true descriptor"
            )

        monkeypatch.setattr(
            "scripts.release_gate.processes.subprocess.Popen", _fail_if_spawned
        )
        profile_env = {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}

        with pytest.raises(StartupError):
            start_gunicorn(profile_env, wrapped, tmp_path / "closed-lying.log")
    finally:
        real_listener.sock.close()


def test_start_gunicorn_closes_parent_duplicate_after_successful_inheritance(
    tmp_path, monkeypatch
):
    """The parent's OWN duplicated descriptor must be closed once
    Gunicorn has inherited its copy -- no descriptor leak in the
    release-gate process itself."""
    listener = reserve_listener()
    recorded_dups = []
    real_dup = os.dup

    def _spy_dup(fd):
        new_fd = real_dup(fd)
        recorded_dups.append(new_fd)
        return new_fd

    monkeypatch.setattr("scripts.release_gate.processes.os.dup", _spy_dup)
    profile_env = {
        "DJANGO_SETTINGS_MODULE": "linuxlab.settings.central",
        "POSTGRES_DB": os.environ.get("POSTGRES_DB", "linuxlab_central_dev"),
    }

    managed = start_gunicorn(profile_env, listener, tmp_path / "fd-leak-success.log")
    try:
        assert len(recorded_dups) == 1
        with pytest.raises(OSError):
            os.fstat(recorded_dups[0])
    finally:
        stop_process(managed, graceful_seconds=5)


def test_start_gunicorn_closes_parent_duplicate_after_validation_failure(
    tmp_path, monkeypatch
):
    """A duplicate created for a listener that then fails validation
    (here: not bound to loopback) must still be closed -- no leak on
    the failure path either."""
    listener = reserve_listener(host="0.0.0.0")
    recorded_dups = []
    real_dup = os.dup

    def _spy_dup(fd):
        new_fd = real_dup(fd)
        recorded_dups.append(new_fd)
        return new_fd

    monkeypatch.setattr("scripts.release_gate.processes.os.dup", _spy_dup)

    def _fail_if_spawned(*args, **kwargs):
        raise AssertionError("must not spawn Gunicorn for a non-loopback listener")

    monkeypatch.setattr(
        "scripts.release_gate.processes.subprocess.Popen", _fail_if_spawned
    )
    profile_env = {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}

    try:
        with pytest.raises(StartupError):
            start_gunicorn(profile_env, listener, tmp_path / "fd-leak-failure.log")
        assert len(recorded_dups) == 1
        with pytest.raises(OSError):
            os.fstat(recorded_dups[0])
    finally:
        listener.sock.close()


def test_start_gunicorn_refuses_a_closed_listener_socket(tmp_path, monkeypatch):
    listener = reserve_listener()
    listener.sock.close()

    def _fail_if_spawned(*args, **kwargs):
        raise AssertionError("must not spawn Gunicorn against a closed listener socket")

    monkeypatch.setattr("scripts.release_gate.processes.subprocess.Popen", _fail_if_spawned)
    profile_env = {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}

    with pytest.raises(StartupError):
        start_gunicorn(profile_env, listener, tmp_path / "closed.log")


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
