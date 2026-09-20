"""Corrective B Task 13: the release-gate target-process bridge
(scripts.release_gate.diagnostics) over the cleared Task-12 canonical
validator. Tests here use a REAL ephemeral Node database and profile
(one migrated database, no Gunicorn) so the target process runs the real
canonical context/providers/validator; only the inner *command* runner is
replaced at the subprocess boundary where a test needs corrupt command
output. The canonical validator is never faked."""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from linuxlab.core.release_diagnostics import ReleaseDiagnosticsError
from scripts.release_gate.database import (
    GateDatabaseNames,
    create_database,
    drop_database,
    run_migrations,
)
from scripts.release_gate.e2e_support import REPO_ROOT, db_admin_from_env
from scripts.release_gate.environment import generate_run_id
from scripts.release_gate.signing_material import create_ephemeral_signing_keypair

SECRET_SENTINEL = "TARGET_SECRET_SENTINEL_9a1c"
NODE_ENV_BASE = {
    "DJANGO_SETTINGS_MODULE": "linuxlab.settings.node",
    "LINUXLAB_SYNC_ENABLED": "true",
    "LINUXLAB_SYNC_SECRET": SECRET_SENTINEL,
    "LINUXLAB_CENTRAL_URL": "http://127.0.0.1:1",
    "LINUXLAB_SYNC_ALLOW_INSECURE_HTTP": "true",
    "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE": "false",
}


@pytest.fixture(scope="module")
def node_target(tmp_path_factory):
    """One real migrated ephemeral Node database plus fixture keys."""
    run_id = generate_run_id()
    names = GateDatabaseNames.for_run(run_id)
    db_admin = db_admin_from_env()
    workspace = tmp_path_factory.mktemp("node-target")
    create_database(db_admin, names, "node")
    try:
        run_migrations(
            names, "node", {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.node"}, repo_root=REPO_ROOT
        )
        signing_path, trust_path = create_ephemeral_signing_keypair(workspace)
        env = {**NODE_ENV_BASE, "POSTGRES_DB": names.node, "LINUXLAB_NODE_ID": str(uuid.uuid4())}
        yield SimpleNamespace(env=env, trust_path=trust_path, signing_path=signing_path, names=names)
    finally:
        drop_database(db_admin, names, "node")


# A supervisor stand-in: runs the REAL target_release_diagnostics() (real
# django.setup, real context/providers/validator in the Node target
# process) with ONLY the inner command runner replaced by a canned
# outcome supplied through argv -- the exact subprocess boundary.
_CORRUPT_TARGET_SCRIPT = """
import json, sys
from scripts.release_gate import diagnostics
canned = json.loads(sys.argv[1])
def _canned_run(argv, *, env, stdin_text, timeout_seconds, own_group):
    return diagnostics.CommandOutcome(
        returncode=canned["returncode"], stdout=canned["stdout"].encode("utf-8"), stderr=b""
    )
diagnostics._run_command = _canned_run
sys.exit(diagnostics.main(sys.argv[2:]))
"""


def _corrupt_supervisor(monkeypatch, *, stdout, returncode=0):
    from scripts.release_gate import diagnostics

    canned = json.dumps({"stdout": stdout, "returncode": returncode})
    monkeypatch.setattr(
        diagnostics, "_supervisor_command", lambda: [sys.executable, "-c", _CORRUPT_TARGET_SCRIPT, canned]
    )


def _report(profile="node", mode="release", overall="PASS", checks=()):
    return json.dumps(
        {
            "schema_version": 1,
            "profile": profile,
            "mode": mode,
            "overall_status": overall,
            "checks": list(checks),
        }
    )


def test_target_bridge_rejects_missing_required_code(node_target, monkeypatch):
    from scripts.release_gate.diagnostics import run_release_diagnostics

    # Otherwise valid canonical report, but missing the TRUST_KEY the real
    # Node target's own providers require (CAN_IMPORT_PACKAGES on node).
    _corrupt_supervisor(
        monkeypatch,
        stdout=_report(
            checks=[
                {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
                {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
                {"code": "NODE_REGISTRATION", "status": "WARN", "message": "local", "details": {"authority_verified": False}},
                {"code": "SYNC_RETRY_POLICY", "status": "PASS", "message": "ok"},
            ],
            overall="WARN",
        ),
    )
    with pytest.raises(ReleaseDiagnosticsError):
        run_release_diagnostics(
            node_target.env,
            expected_profile="node",
            repo_root=REPO_ROOT,
            fixture_key_paths=(None, node_target.trust_path),
        )


# --- real end-to-end chain: subprocess -> Task-12 validator -> gate decision --


def test_real_node_target_release_chain_returns_warn_report(node_target):
    from scripts.release_gate.diagnostics import run_release_diagnostics

    report = run_release_diagnostics(
        node_target.env, expected_profile="node", repo_root=REPO_ROOT,
        fixture_key_paths=(None, node_target.trust_path),
    )

    checks = {c.code: c for c in report.checks}
    assert report.profile == "node"
    assert report.mode == "release"
    assert report.overall_status == "WARN"
    assert checks["TRUST_KEY"].status == "PASS"
    assert checks["NODE_REGISTRATION"].status == "WARN"
    assert checks["SYNC_RETRY_POLICY"].status == "PASS"
    assert checks["DB_CONNECTIVITY"].status == "PASS"
    assert checks["SCHEMA_STATE"].status == "PASS"
    assert "CENTRAL_CONNECTIVITY" not in checks  # policy false: no probe
    assert SECRET_SENTINEL not in repr(report)


def test_real_node_target_without_fixture_key_fails_on_missing_trust_key(node_target):
    from scripts.release_gate.diagnostics import run_release_diagnostics

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)
    assert SECRET_SENTINEL not in str(caught.value) + repr(caught.value)


def test_malformed_release_switch_fails_the_gate_without_network(node_target):
    from scripts.release_gate.diagnostics import run_release_diagnostics

    env = {**node_target.env, "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE": "maybe"}
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        run_release_diagnostics(
            env, expected_profile="node", repo_root=REPO_ROOT, fixture_key_paths=(None, node_target.trust_path)
        )
    assert "maybe" not in str(caught.value)


def test_mismatched_expected_profile_is_rejected_by_the_target(node_target):
    from scripts.release_gate.diagnostics import run_release_diagnostics

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        run_release_diagnostics(
            node_target.env, expected_profile="central", repo_root=REPO_ROOT,
            fixture_key_paths=(node_target.signing_path, None),
        )
    assert str(caught.value) == "release diagnostics target process failed"


# --- corrupt inner command output is rejected before export to the parent --

_LOCAL_OK_CHECKS = [
    {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
    {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
    {"code": "TRUST_KEY", "status": "PASS", "message": "ok"},
    {"code": "NODE_REGISTRATION", "status": "WARN", "message": "local", "details": {"authority_verified": False}},
    {"code": "SYNC_RETRY_POLICY", "status": "PASS", "message": "ok"},
]


@pytest.mark.parametrize(
    "stdout,returncode",
    [
        ("not json", 0),
        ("", 0),
        (_report(checks=_LOCAL_OK_CHECKS, overall="WARN")[:-1] + ',"checks":[]}', 0),  # duplicate member
        (_report(checks=_LOCAL_OK_CHECKS + [_LOCAL_OK_CHECKS[2]], overall="WARN"), 0),  # duplicate code
        (_report(profile="central", checks=_LOCAL_OK_CHECKS, overall="WARN"), 0),
        (_report(mode="local", checks=_LOCAL_OK_CHECKS, overall="WARN"), 0),
        (_report(checks=_LOCAL_OK_CHECKS, overall="PASS"), 0),  # inconsistent overall
        (_report(checks=_LOCAL_OK_CHECKS, overall="WARN"), 1),  # nonzero command exit
        (_report(checks=[{**c, "status": "FAIL"} if c["code"] == "TRUST_KEY" else c for c in _LOCAL_OK_CHECKS], overall="FAIL"), 1),
        (_report(checks=_LOCAL_OK_CHECKS, overall="WARN") + " " * 70000, 0),  # oversized
    ],
)
def test_corrupt_command_output_is_rejected_by_the_target(node_target, monkeypatch, stdout, returncode):
    from scripts.release_gate.diagnostics import run_release_diagnostics

    _corrupt_supervisor(monkeypatch, stdout=stdout, returncode=returncode)
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        run_release_diagnostics(
            node_target.env, expected_profile="node", repo_root=REPO_ROOT,
            fixture_key_paths=(None, node_target.trust_path),
        )
    assert "not json" not in str(caught.value)


def test_warn_command_output_passes_through_the_target(node_target, monkeypatch):
    from scripts.release_gate.diagnostics import run_release_diagnostics

    _corrupt_supervisor(monkeypatch, stdout=_report(checks=_LOCAL_OK_CHECKS, overall="WARN"))
    report = run_release_diagnostics(
        node_target.env, expected_profile="node", repo_root=REPO_ROOT,
        fixture_key_paths=(None, node_target.trust_path),
    )
    assert report.overall_status == "WARN"


# --- parent-side supervisor failure paths ----------------------------------------

_SLEEPING_SUPERVISOR = """
import os, sys, time, subprocess
pid_dir = sys.argv[1]
open(os.path.join(pid_dir, "supervisor.pid"), "w").write(str(os.getpid()))
child = subprocess.Popen([sys.executable, "-c", "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(600)", os.path.join(pid_dir, "child.pid")])
child.wait()
"""

_OVERSIZED_SUPERVISOR = """
import os, sys, time
open(sys.argv[1], "w").write(str(os.getpid()))
sys.stdout.write("x" * 70000)
sys.stdout.flush()
time.sleep(600)
"""


def _wait_gone(pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # A killed-but-not-yet-reaped child of *our* process would still answer
        # kill(0); reap via waitpid when it is ours, otherwise keep polling.
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return True
        except ChildProcessError:
            pass
        time.sleep(0.05)
    return False


def test_supervisor_timeout_kills_the_whole_process_group(node_target, monkeypatch, tmp_path):
    from scripts.release_gate import diagnostics

    monkeypatch.setattr(
        diagnostics, "_supervisor_command", lambda: [sys.executable, "-c", _SLEEPING_SUPERVISOR, str(tmp_path)]
    )
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 2)

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert str(caught.value) == "release diagnostics target process timed out"
    supervisor_pid = int((tmp_path / "supervisor.pid").read_text())
    child_pid = int((tmp_path / "child.pid").read_text())
    assert _wait_gone(supervisor_pid)
    assert _wait_gone(child_pid)


def test_supervisor_output_overflow_is_rejected_and_process_stopped(node_target, monkeypatch, tmp_path):
    from scripts.release_gate import diagnostics

    pid_file = tmp_path / "pid"
    monkeypatch.setattr(
        diagnostics, "_supervisor_command", lambda: [sys.executable, "-c", _OVERSIZED_SUPERVISOR, str(pid_file)]
    )

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert str(caught.value) == "release diagnostics target process produced oversized output"
    assert "xxx" not in str(caught.value) + repr(caught.value)
    assert _wait_gone(int(pid_file.read_text()))


def test_supervisor_launch_failure_is_a_safe_error(node_target, monkeypatch, tmp_path):
    from scripts.release_gate import diagnostics

    monkeypatch.setattr(diagnostics, "_supervisor_command", lambda: [str(tmp_path / "missing-interpreter")])
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)
    assert str(caught.value) == "release diagnostics target process could not be launched"
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("profile", ["test", "", None, "Node"])
def test_parent_rejects_non_deployment_expected_profile_before_launch(node_target, monkeypatch, profile):
    from scripts.release_gate import diagnostics

    monkeypatch.setattr(diagnostics, "_run_command", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ReleaseDiagnosticsError):
        diagnostics.run_release_diagnostics(node_target.env, expected_profile=profile, repo_root=REPO_ROOT)


@pytest.mark.parametrize(
    "paths",
    [
        (Path("/tmp/signing.key"), None),  # signing path on a node target
        (None, None, None),
        ("only-one",),
        (None, 42),
    ],
)
def test_parent_rejects_fixture_paths_that_do_not_match_the_profile(node_target, monkeypatch, paths):
    from scripts.release_gate import diagnostics

    monkeypatch.setattr(diagnostics, "_run_command", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ReleaseDiagnosticsError):
        diagnostics.run_release_diagnostics(
            node_target.env, expected_profile="node", repo_root=REPO_ROOT, fixture_key_paths=paths
        )


def test_supervisor_argv_is_an_argument_array_with_the_fixed_module_entry(node_target, monkeypatch):
    from scripts.release_gate import diagnostics

    captured = {}

    def _capture(argv, *, env, stdin_text, timeout_seconds, own_group):
        captured.update(argv=list(argv), stdin=stdin_text, timeout=timeout_seconds, group=own_group)
        return diagnostics.CommandOutcome(returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(diagnostics, "_run_command", _capture)
    with pytest.raises(ReleaseDiagnosticsError):
        diagnostics.run_release_diagnostics(
            node_target.env, expected_profile="node", repo_root=REPO_ROOT,
            fixture_key_paths=(None, node_target.trust_path),
        )
    assert captured["argv"] == [sys.executable, "-m", "scripts.release_gate.diagnostics", "--expected-profile", "node"]
    assert json.loads(captured["stdin"]) == {"trust_anchor_path": str(node_target.trust_path)}
    assert captured["timeout"] == 120
    assert captured["group"] is True


# --- cli.py diagnose-release: raw canonical stdout, real exit code -----------


def _cli(env, stdin_payload):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "release_gate" / "cli.py"), "diagnose-release"],
        cwd=str(REPO_ROOT), env={**os.environ, **env}, input=json.dumps(stdin_payload) if stdin_payload is not None else "",
        capture_output=True, text=True, timeout=120, check=False,
    )


def test_cli_diagnose_release_writes_exactly_one_canonical_report(node_target):
    result = _cli(node_target.env, {"trust_anchor_path": str(node_target.trust_path)})
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report) == {"schema_version", "profile", "mode", "overall_status", "checks"}
    assert report["mode"] == "release" and report["overall_status"] == "WARN"
    assert result.stdout.count("\n") <= 1
    assert "error" not in result.stdout
    assert SECRET_SENTINEL not in result.stdout + result.stderr


def test_cli_diagnose_release_fail_report_keeps_command_returncode(node_target):
    result = _cli(node_target.env, {})  # no trust anchor -> TRUST_KEY FAIL
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["overall_status"] == "FAIL"
    assert result.stderr.strip() == "diagnose-release: deployment diagnostics did not pass"
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("payload", [None, "[]", {"trust_anchor_path": 5}])
def test_cli_diagnose_release_bad_stdin_is_a_fixed_safe_failure(node_target, payload):
    if payload == "[]":
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "release_gate" / "cli.py"), "diagnose-release"],
            cwd=str(REPO_ROOT), env={**os.environ, **node_target.env}, input="[]",
            capture_output=True, text=True, timeout=120, check=False,
        )
    else:
        result = _cli(node_target.env, payload)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "diagnose-release: could not run deployment diagnostics"


# --- boundaries: no env-policy parsing in scripts; no production import of harness


def test_bridge_parses_no_release_policy_from_the_environment():
    source = (REPO_ROOT / "scripts" / "release_gate" / "diagnostics.py").read_text()
    assert "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE" not in source
    assert "LINUXLAB_SYNC_ENABLED" not in source
    assert 'os.environ.get("LINUXLAB_' not in source
    assert "shell=True" not in source


def test_production_code_never_imports_the_release_harness():
    offenders = []
    for path in (REPO_ROOT / "linuxlab").rglob("*.py"):
        if "/tests/" in str(path):
            continue
        text = path.read_text()
        if "from scripts" in text or "import scripts" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


# --- Corrective (T13-I1, T13-C-I1, T13-C-I2): process-tree containment ------
#
# Topology under test: the harness owns ONE session/process group led by the
# supervisor; the inner diagnostic child and everything it spawns stay in
# that group. Every failure path -- and the success path -- must leave no
# live process owned by the execution when the bridge returns, enforced by
# the PARENT (no supervisor cooperation required). Tests use real
# processes, PID/PGID/SID evidence files, and assert BEFORE any emergency
# cleanup; emergency cleanup exists only to protect the developer machine.

_IDENTITY_SNIPPET = """
def _identity(path, pid):
    import json, os
    with open(path, "w") as handle:
        json.dump({"pid": pid, "pgid": os.getpgid(pid), "sid": os.getsid(pid)}, handle)
"""

_CHILD_SCRIPT = """
import json, os, signal, subprocess, sys, time
pid_dir, mode = sys.argv[1], sys.argv[2]
def _identity(path, pid):
    with open(path, "w") as handle:
        json.dump({"pid": pid, "pgid": os.getpgid(pid), "sid": os.getsid(pid)}, handle)
resistant = mode in ("sigterm-resistant", "success-daemon")
transient = mode == "transient-descendant"
descendant_code = "import signal, time; " + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if resistant else "") + ("time.sleep(0.05)" if transient else "time.sleep(600)")
descendant = subprocess.Popen([sys.executable, "-c", descendant_code])
if resistant:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
_identity(os.path.join(pid_dir, "descendant.json"), descendant.pid)
_identity(os.path.join(pid_dir, "child.json"), os.getpid())
if mode == "overflow":
    sys.stdout.write("x" * 70000); sys.stdout.flush()
elif mode == "stderr-overflow":
    sys.stderr.write("y" * 70000); sys.stderr.flush()
elif mode == "nonzero":
    sys.exit(3)  # leaves the descendant running: production must clean it
elif mode == "transient-descendant":
    descendant.wait()  # the descendant is already gone before the report is written
    sys.stdout.write(json.dumps({"schema_version": 1, "profile": "node", "mode": "release", "overall_status": "WARN", "checks": [
        {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
        {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
        {"code": "TRUST_KEY", "status": "PASS", "message": "ok"},
        {"code": "NODE_REGISTRATION", "status": "WARN", "message": "local", "details": {"authority_verified": False}},
        {"code": "SYNC_RETRY_POLICY", "status": "PASS", "message": "ok"}]}))
    sys.stdout.flush()
    sys.exit(0)
elif mode == "success-daemon":
    sys.stdout.write(json.dumps({"schema_version": 1, "profile": "node", "mode": "release", "overall_status": "WARN", "checks": [
        {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
        {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
        {"code": "TRUST_KEY", "status": "PASS", "message": "ok"},
        {"code": "NODE_REGISTRATION", "status": "WARN", "message": "local", "details": {"authority_verified": False}},
        {"code": "SYNC_RETRY_POLICY", "status": "PASS", "message": "ok"}]}))
    sys.stdout.flush()
    sys.exit(0)  # valid report, but a daemonized SIGTERM-ignoring descendant remains
time.sleep(600)
"""

# A supervisor stand-in that runs the REAL target (real django.setup, real
# _run_command with its real timeout/overflow/kill/sweep logic) but points
# the inner diagnostic child at _CHILD_SCRIPT, which spawns a sleeping
# DESCENDANT, records identities, then behaves per `mode`. Only the child
# argv and the inner timeout are replaced.
_DESCENDANT_TARGET_SCRIPT = _IDENTITY_SNIPPET + """
import json, os, sys, threading
from scripts.release_gate import diagnostics
mode, pid_dir, child_script = sys.argv[1], sys.argv[2], sys.argv[3]
_identity(os.path.join(pid_dir, "supervisor.json"), os.getpid())
diagnostics._child_invocation = lambda paths: ([sys.executable, "-c", child_script, pid_dir, mode], "")
diagnostics.CHILD_TIMEOUT_SECONDS = 2
if mode == "target-reader-failure":
    def _boom(self):
        raise RuntimeError("READER_START_SENTINEL")
    threading.Thread.start = _boom
sys.exit(diagnostics.main(sys.argv[4:]))
"""


def _descendant_supervisor(monkeypatch, mode, pid_dir):
    from scripts.release_gate import diagnostics

    monkeypatch.setattr(
        diagnostics,
        "_supervisor_command",
        lambda: [sys.executable, "-c", _DESCENDANT_TARGET_SCRIPT, mode, str(pid_dir), _CHILD_SCRIPT],
    )
    monkeypatch.setattr(diagnostics, "_TERMINATION_GRACE_SECONDS", 1)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # our own killed-but-unreaped child answers kill(0); reap it if it is ours
        waited, _status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    return True


def _identity(pid_dir, name, timeout=15.0):
    deadline = time.monotonic() + timeout
    path = pid_dir / name
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass
        time.sleep(0.05)
    raise AssertionError(f"{name} never written")


@pytest.fixture
def sentinel_process():
    """An UNRELATED process in its own session: must survive every cleanup."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
    try:
        yield process
    finally:
        process.kill()
        process.wait(timeout=10)


def _emergency_cleanup(*pids):
    for pid in pids:
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass


def _optional_pid(pid_dir, name, wait):
    try:
        return _identity(pid_dir, name, timeout=wait)["pid"]
    except AssertionError:
        return None


def _assert_tree_gone(pid_dir, *, tree_may_be_unformed=False):
    """Assert BEFORE any emergency cleanup; clean up only if the assertion
    fails. With `tree_may_be_unformed`, the child can have been killed
    before it ever recorded itself or its descendant (production cleanup
    struck within milliseconds of spawn); whatever WAS recorded must be gone."""
    supervisor = _identity(pid_dir, "supervisor.json")["pid"]
    wait = 1.0 if tree_may_be_unformed else 15.0
    descendant = _optional_pid(pid_dir, "descendant.json", wait)
    child = _optional_pid(pid_dir, "child.json", wait)
    if not tree_may_be_unformed:
        assert descendant is not None and child is not None
    pids = [p for p in (supervisor, child, descendant) if p is not None]
    try:
        for pid in pids:
            assert _wait_gone(pid, timeout=5.0), (
                f"pid {pid} survived (supervisor={supervisor}, child={child}, descendant={descendant})"
            )
    except AssertionError:
        _emergency_cleanup(*pids)
        raise


@pytest.mark.parametrize("mode", ["timeout", "overflow", "stderr-overflow", "nonzero", "sigterm-resistant"])
def test_inner_failure_kills_every_owned_descendant(node_target, monkeypatch, tmp_path, mode, sentinel_process):
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, mode, tmp_path)
    started = time.monotonic()

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert str(caught.value) == "release diagnostics target process failed"
    assert "xxx" not in str(caught.value) and "yyy" not in str(caught.value)
    assert time.monotonic() - started < 60
    _assert_tree_gone(tmp_path)
    assert _alive(sentinel_process.pid)
    assert not hasattr(diagnostics, "_ACTIVE_CHILD_GROUPS")


def test_success_with_a_daemonized_descendant_fails_closed(node_target, monkeypatch, tmp_path, sentinel_process):
    """A valid report cannot buy success if the execution left an unowned
    live process behind (no detached-child use case exists in authority)."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "success-daemon", tmp_path)
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)
    assert str(caught.value) == "release diagnostics target left stray processes"
    _assert_tree_gone(tmp_path)
    assert _alive(sentinel_process.pid)


def test_target_reader_start_failure_after_spawn_leaves_no_survivor(node_target, monkeypatch, tmp_path, sentinel_process):
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "target-reader-failure", tmp_path)
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)
    assert str(caught.value) == "release diagnostics target process failed"
    assert "READER_START_SENTINEL" not in str(caught.value)
    _assert_tree_gone(tmp_path, tree_may_be_unformed=True)
    assert _alive(sentinel_process.pid)


def test_parent_reader_start_failure_after_spawn_leaves_no_survivor(node_target, monkeypatch, tmp_path, sentinel_process):
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "timeout", tmp_path)

    class _BrokenThread(threading.Thread):
        def start(self):
            raise RuntimeError("PARENT_READER_SENTINEL")

    spawned = []
    real_popen = diagnostics.subprocess.Popen

    def _recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process.pid)
        return process

    monkeypatch.setattr(diagnostics.threading, "Thread", _BrokenThread)
    monkeypatch.setattr(diagnostics.subprocess, "Popen", _recording_popen)
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)
    assert str(caught.value) == "release diagnostics process supervision failed"
    assert "PARENT_READER_SENTINEL" not in str(caught.value) + repr(caught.value)
    # Ownership began at Popen: the supervisor was killed and reaped before
    # it could even record itself, and its group holds nothing else.
    assert len(spawned) == 1
    try:
        assert _wait_gone(spawned[0], timeout=5.0)
        assert not diagnostics._signal_group(spawned[0], 0)
    except AssertionError:
        _emergency_cleanup(spawned[0])
        raise
    assert _alive(sentinel_process.pid)


def test_unresponsive_supervisor_timeout_is_parent_enforced(node_target, monkeypatch, tmp_path, sentinel_process):
    """T13-C-I2: the supervisor is SIGSTOP'd (it can execute NO Python, no
    signal handler); the parent's timeout escalation alone must remove the
    supervisor, the inner child and the descendant. Identity evidence for
    every layer is recorded for review."""
    import signal as _signal

    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "timeout", tmp_path)
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 3)
    stopped = {}

    def _stop_supervisor_once_tree_exists():
        descendant = _identity(tmp_path, "descendant.json")
        supervisor = _identity(tmp_path, "supervisor.json")
        os.kill(supervisor["pid"], _signal.SIGSTOP)
        stopped.update(supervisor=supervisor, child=_identity(tmp_path, "child.json"), descendant=descendant)

    stopper = threading.Thread(target=_stop_supervisor_once_tree_exists, daemon=True)
    stopper.start()
    started = time.monotonic()
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)
    stopper.join(timeout=10)
    elapsed = time.monotonic() - started

    assert str(caught.value) == "release diagnostics target process timed out"
    assert elapsed < 30
    assert stopped, "supervisor was never stopped"
    harness = {"pid": os.getpid(), "pgid": os.getpgid(0), "sid": os.getsid(0)}
    (tmp_path / "identity-evidence.json").write_text(json.dumps({"harness": harness, **stopped}, indent=2))
    # Ownership model: the supervisor leads its own session/group; the child
    # and descendant share it; the harness is outside it.
    assert stopped["supervisor"]["pgid"] == stopped["supervisor"]["pid"]
    assert stopped["child"]["pgid"] == stopped["supervisor"]["pgid"]
    assert stopped["descendant"]["pgid"] == stopped["supervisor"]["pgid"]
    assert harness["pgid"] != stopped["supervisor"]["pgid"]
    _assert_tree_gone(tmp_path)
    assert _alive(sentinel_process.pid)


def test_sigterm_ignoring_supervisor_is_killed_by_escalation(node_target, monkeypatch, tmp_path, sentinel_process):
    from scripts.release_gate import diagnostics

    script = _IDENTITY_SNIPPET + """
import os, signal, subprocess, sys, time
pid_dir = sys.argv[1]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
_identity(os.path.join(pid_dir, "supervisor.json"), os.getpid())
child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"])
_identity(os.path.join(pid_dir, "child.json"), child.pid)
_identity(os.path.join(pid_dir, "descendant.json"), child.pid)
time.sleep(600)
"""
    monkeypatch.setattr(diagnostics, "_supervisor_command", lambda: [sys.executable, "-c", script, str(tmp_path)])
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(diagnostics, "_TERMINATION_GRACE_SECONDS", 1)
    started = time.monotonic()
    with pytest.raises(ReleaseDiagnosticsError):
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)
    assert time.monotonic() - started < 30
    _assert_tree_gone(tmp_path)
    assert _alive(sentinel_process.pid)


def test_successful_real_command_is_not_killed_prematurely(node_target, sentinel_process):
    """The real chain with the real command: no false timeout/overflow kill,
    no stray-process false positive, canonical WARN report still returned."""
    from scripts.release_gate.diagnostics import run_release_diagnostics

    report = run_release_diagnostics(
        node_target.env, expected_profile="node", repo_root=REPO_ROOT,
        fixture_key_paths=(None, node_target.trust_path),
    )
    assert report.overall_status == "WARN"
    assert _alive(sentinel_process.pid)


# --- Corrective (T13-C-I1-A, T13-C-I1-B): post-Popen ownership protection and
# exception-proof, monotonic cleanup ------------------------------------------
#
# Every case below builds a REAL tree (supervisor -> inner child ->
# descendant), injects a failure at exactly one setup/observation point, and
# asserts production cleanup BEFORE any emergency cleanup.

SETUP_SENTINEL = "SETUP_FAILURE_SENTINEL_6b2f"
POLL_SENTINEL = "POLL_FAILURE_SENTINEL_9c41"
WAIT_SENTINEL = "WAIT_FAILURE_SENTINEL_2ad8"
SIGNAL_SENTINEL = "SIGNAL_FAILURE_SENTINEL_71ce"
JOIN_SENTINEL = "JOIN_FAILURE_SENTINEL_5fd0"


def _record_spawned(monkeypatch, *, sabotage_poll=False, sabotage_wait=False):
    """Wrap the parent's Popen so the test knows every PID production
    spawned, and optionally sabotage that instance's own observation
    methods (never the whole class, never the process tree itself)."""
    from scripts.release_gate import diagnostics

    spawned: list = []
    real_popen = diagnostics.subprocess.Popen

    def _factory(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process.pid)
        if sabotage_poll:
            def _failing_poll():
                raise RuntimeError(POLL_SENTINEL)

            process.poll = _failing_poll
        if sabotage_wait:
            def _failing_wait(timeout=None):
                raise RuntimeError(WAIT_SENTINEL)

            process.wait = _failing_wait
        return process

    monkeypatch.setattr(diagnostics.subprocess, "Popen", _factory)
    return spawned


def _wait_group_gone(pgid, timeout=10.0):
    """Poll (bounded) until killpg(pgid, 0) reports no member of the
    group is signalable any more -- SIGKILL delivery/reaping is not
    instantaneous even though it cannot be blocked, so a single
    immediate probe right after a kill is a real race, not evidence of
    survival."""
    from scripts.release_gate import diagnostics

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not diagnostics._signal_group(pgid, 0):
            return True
        time.sleep(0.05)
    return not diagnostics._signal_group(pgid, 0)


def _assert_spawned_gone(spawned, *, sentinel_process, extra_pids=()):
    """Assert BEFORE emergency cleanup: nothing production spawned (and
    nothing it owned) is alive, and its process group is empty."""
    pids = [*spawned, *extra_pids]
    try:
        for pid in spawned:
            assert _wait_gone(pid, timeout=10.0), f"owned process {pid} survived"
            assert _wait_group_gone(pid, timeout=10.0), f"owned group {pid} survived"
        for pid in extra_pids:
            assert _wait_gone(pid, timeout=10.0), f"owned descendant {pid} survived"
        assert _alive(sentinel_process.pid)
    except AssertionError:
        _emergency_cleanup(*pids)
        raise


def test_event_construction_failure_after_spawn_leaves_no_survivor(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    """T13-C-I1-A: the fallible setup that used to sit between a successful
    Popen and the protected scope must never abandon a live tree."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "timeout", tmp_path)
    spawned = _record_spawned(monkeypatch)

    def _failing_event():
        raise RuntimeError(SETUP_SENTINEL)

    monkeypatch.setattr(diagnostics.threading, "Event", _failing_event)
    started = time.monotonic()

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert time.monotonic() - started < 60
    assert SETUP_SENTINEL not in str(caught.value) + repr(caught.value)
    assert str(caught.value) in {
        "release diagnostics process supervision failed",
        "release diagnostics target process could not be launched",
    }
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process)


def test_thread_constructor_failure_after_spawn_leaves_no_survivor(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    """Thread construction genuinely requires the child's pipes, so it can
    only happen after Popen: the protected scope must contain it."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "timeout", tmp_path)
    spawned = _record_spawned(monkeypatch)

    class _BrokenThread:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(SETUP_SENTINEL)

    monkeypatch.setattr(diagnostics.threading, "Thread", _BrokenThread)

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert str(caught.value) == "release diagnostics process supervision failed"
    assert SETUP_SENTINEL not in str(caught.value) + repr(caught.value)
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process)


def test_persistent_poll_failure_still_contains_the_tree(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    """T13-C-I1-B: liveness observation is never a gate on termination."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "timeout", tmp_path)
    spawned = _record_spawned(monkeypatch, sabotage_poll=True)
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 3)

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    # A persistently failing poll() means "cannot confirm exit" is treated
    # as "still running" (never as a skip signal) -- the loop legitimately
    # runs to its deadline and reports a genuine, contained timeout. Either
    # fixed outcome is acceptable; what matters is containment and no leak.
    assert str(caught.value) in {
        "release diagnostics process supervision failed",
        "release diagnostics target process timed out",
    }
    assert POLL_SENTINEL not in str(caught.value) + repr(caught.value)
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process)


def test_reap_failure_does_not_abort_kill_escalation(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    """T13-C-I1-B, mandatory case: a SIGTERM-resistant tree plus a wait/reap
    failure during the grace path -- SIGKILL escalation must still happen."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "sigterm-resistant", tmp_path)
    spawned = _record_spawned(monkeypatch, sabotage_wait=True)
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(diagnostics, "_TERMINATION_GRACE_SECONDS", 1)
    started = time.monotonic()

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert time.monotonic() - started < 60
    assert WAIT_SENTINEL not in str(caught.value) + repr(caught.value)
    assert str(caught.value) in {
        "release diagnostics target process timed out",
        "release diagnostics process supervision failed",
        "release diagnostics target left stray processes",
    }
    child = _optional_pid(tmp_path, "child.json", 10.0)
    descendant = _optional_pid(tmp_path, "descendant.json", 10.0)
    extra = [pid for pid in (child, descendant) if pid is not None]
    assert extra, "the resistant inner tree never recorded itself"
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process, extra_pids=extra)


def test_first_signal_error_does_not_prevent_later_cleanup(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    """A single failed signal attempt must not abort the sequence: later,
    stronger attempts still run and the tree is contained."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "timeout", tmp_path)
    spawned = _record_spawned(monkeypatch)
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 3)
    real_killpg = diagnostics.os.killpg
    calls: list = []

    def _flaky_killpg(pgid, signum):
        calls.append((pgid, signum))
        if len(calls) == 1:
            raise OSError(SIGNAL_SENTINEL)
        return real_killpg(pgid, signum)

    monkeypatch.setattr(diagnostics.os, "killpg", _flaky_killpg)

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert SIGNAL_SENTINEL not in str(caught.value) + repr(caught.value)
    assert len(calls) > 1, "cleanup stopped at the first failed signal"
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process)


def test_reader_join_failure_does_not_abort_containment(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "timeout", tmp_path)
    spawned = _record_spawned(monkeypatch)
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 3)

    class _UnjoinableThread(threading.Thread):
        def join(self, timeout=None):
            raise RuntimeError(JOIN_SENTINEL)

    monkeypatch.setattr(diagnostics.threading, "Thread", _UnjoinableThread)

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert JOIN_SENTINEL not in str(caught.value) + repr(caught.value)
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process)


def test_transient_descendant_that_exits_does_not_fail_a_valid_report(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    """Authority nuance: only a SURVIVING stray converts success into the
    stray-process failure. A descendant that already exited leaves the
    valid report intact."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "transient-descendant", tmp_path)
    spawned = _record_spawned(monkeypatch)

    report = diagnostics.run_release_diagnostics(
        node_target.env, expected_profile="node", repo_root=REPO_ROOT
    )

    assert report.overall_status == "WARN"
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process)


def test_successful_command_leaves_no_reader_thread_or_owned_group(node_target, sentinel_process):
    from scripts.release_gate import diagnostics

    before = threading.active_count()
    report = diagnostics.run_release_diagnostics(
        node_target.env, expected_profile="node", repo_root=REPO_ROOT,
        fixture_key_paths=(None, node_target.trust_path),
    )
    assert report.overall_status == "WARN"
    deadline = time.monotonic() + 5
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert threading.active_count() <= before
    assert _alive(sentinel_process.pid)


# --- Corrective (T13-C-I1-B, round 3): finalization itself must be
# non-abortable -- ANY exception from ONE finalization phase (sweep,
# residual-group observation, reap, a signal call, a reader join) must
# not prevent every later applicable phase from still running, and must
# never let the bridge conclude success while containment is unproven.

FINAL_SWEEP_SENTINEL = "FINAL_SWEEP_FAILURE_SENTINEL_a37d"
RESIDUAL_OBSERVATION_SENTINEL = "RESIDUAL_OBSERVATION_SENTINEL_c91e"
REAP_FINALIZATION_SENTINEL = "REAP_FINALIZATION_SENTINEL_1b6f"
JOIN_FIRST_SENTINEL = "JOIN_FIRST_SENTINEL_8de2"
SIGNAL_LATER_USABLE_SENTINEL = "SIGNAL_LATER_USABLE_SENTINEL_44aa"


def test_final_sweep_exception_still_kills_descendants_and_joins_readers(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    """Exact reviewer reproduction: inject a failure into the currently
    unprotected final-sweep helper around line 324, with a real
    supervisor/child/descendant tree and real reader threads already
    running. OS kill primitives remain fully usable throughout."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "sigterm-resistant", tmp_path)
    spawned = _record_spawned(monkeypatch)
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(diagnostics, "_TERMINATION_GRACE_SECONDS", 1)

    real_sweep = diagnostics._sweep_owned_group
    calls = []

    def _failing_sweep(pgid):
        calls.append(pgid)
        if len(calls) == 1:
            raise RuntimeError(FINAL_SWEEP_SENTINEL)
        return real_sweep(pgid)  # a later, legitimate call (e.g. from a retry) still works

    monkeypatch.setattr(diagnostics, "_sweep_owned_group", _failing_sweep)
    started = time.monotonic()

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert time.monotonic() - started < 60
    assert FINAL_SWEEP_SENTINEL not in str(caught.value) + repr(caught.value)
    assert "Traceback" not in str(caught.value) + repr(caught.value)
    assert str(caught.value) == "release diagnostics process supervision failed"

    child = _optional_pid(tmp_path, "child.json", 10.0)
    descendant = _optional_pid(tmp_path, "descendant.json", 10.0)
    extra = [pid for pid in (child, descendant) if pid is not None]
    assert extra, "the resistant tree never recorded itself"
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process, extra_pids=extra)

    # The mandatory reader-thread proof: no reader thread from this
    # command's own supervision survives the bridge's return.
    assert not any(t.is_alive() for t in threading.enumerate() if getattr(t, "_target", None) is diagnostics._drain)


def test_residual_group_observation_exception_does_not_abort_finalization(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    """A failure specifically inside the sweep's OWN residual-group
    check (not the outer call site) must still leave later finalization
    phases (reader joins) intact and the tree contained."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "sigterm-resistant", tmp_path)
    spawned = _record_spawned(monkeypatch)
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(diagnostics, "_TERMINATION_GRACE_SECONDS", 1)

    real_signal_group = diagnostics._signal_group
    calls = []

    def _flaky_signal_group(pgid, signum):
        calls.append((pgid, signum))
        if len(calls) == 3:  # let TERM and the first KILL through, then fail once
            raise OSError(RESIDUAL_OBSERVATION_SENTINEL)
        return real_signal_group(pgid, signum)

    monkeypatch.setattr(diagnostics, "_signal_group", _flaky_signal_group)

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert RESIDUAL_OBSERVATION_SENTINEL not in str(caught.value) + repr(caught.value)
    assert len(calls) > 3, "cleanup stopped at the injected failure instead of continuing"
    child = _optional_pid(tmp_path, "child.json", 10.0)
    descendant = _optional_pid(tmp_path, "descendant.json", 10.0)
    extra = [pid for pid in (child, descendant) if pid is not None]
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process, extra_pids=extra)


def test_reap_exception_during_finalization_does_not_abort_later_phases(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "timeout", tmp_path)
    spawned = _record_spawned(monkeypatch)
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 3)

    real_reap = diagnostics._reap
    calls = []

    def _failing_reap(process, timeout):
        calls.append(timeout)
        raise RuntimeError(REAP_FINALIZATION_SENTINEL)

    monkeypatch.setattr(diagnostics, "_reap", _failing_reap)

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert REAP_FINALIZATION_SENTINEL not in str(caught.value) + repr(caught.value)
    assert len(calls) >= 1
    child = _optional_pid(tmp_path, "child.json", 10.0)
    descendant = _optional_pid(tmp_path, "descendant.json", 10.0)
    extra = [pid for pid in (child, descendant) if pid is not None]
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process, extra_pids=extra)
    real_reap(diagnostics.subprocess.Popen(["true"]), 5)  # sanity: real _reap still usable after test


def test_first_reader_join_exception_does_not_skip_the_second(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "timeout", tmp_path)
    spawned = _record_spawned(monkeypatch)
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 3)

    real_thread_cls = diagnostics.threading.Thread
    join_calls: list = []

    class _FirstJoinFails(real_thread_cls):
        def join(self, timeout=None):
            join_calls.append(self)
            if len(join_calls) == 1:
                raise RuntimeError(JOIN_FIRST_SENTINEL)
            return super().join(timeout=timeout)

    monkeypatch.setattr(diagnostics.threading, "Thread", _FirstJoinFails)

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert JOIN_FIRST_SENTINEL not in str(caught.value) + repr(caught.value)
    assert len(join_calls) == 2, "the second reader's join was skipped after the first one raised"
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process)


def test_signal_helper_exception_still_allows_later_stronger_cleanup(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    """A single failing os.killpg() call (an exception type _signal_group
    itself does not catch) must not stop the sequence -- later signal
    attempts made from other finalization phases still run and succeed."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "sigterm-resistant", tmp_path)
    spawned = _record_spawned(monkeypatch)
    monkeypatch.setattr(diagnostics, "SUPERVISOR_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(diagnostics, "_TERMINATION_GRACE_SECONDS", 1)

    real_killpg = diagnostics.os.killpg
    calls = []

    def _flaky_killpg(pgid, signum):
        calls.append((pgid, signum))
        if len(calls) == 1:
            raise RuntimeError(SIGNAL_LATER_USABLE_SENTINEL)
        return real_killpg(pgid, signum)

    monkeypatch.setattr(diagnostics.os, "killpg", _flaky_killpg)

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert SIGNAL_LATER_USABLE_SENTINEL not in str(caught.value) + repr(caught.value)
    assert len(calls) > 1, "cleanup stopped after the first failed signal instead of continuing"
    child = _optional_pid(tmp_path, "child.json", 10.0)
    descendant = _optional_pid(tmp_path, "descendant.json", 10.0)
    extra = [pid for pid in (child, descendant) if pid is not None]
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process, extra_pids=extra)


def test_finalization_failure_never_reports_false_success(
    node_target, monkeypatch, tmp_path, sentinel_process
):
    """Even when the inner command produced a structurally valid PASS/WARN
    report, a finalization phase that raised must still fail the run
    closed -- a valid report alone can never buy success when cleanup
    itself could not be verified complete."""
    from scripts.release_gate import diagnostics

    _descendant_supervisor(monkeypatch, "success-daemon", tmp_path)
    spawned = _record_spawned(monkeypatch)

    def _failing_sweep(pgid):
        raise RuntimeError(FINAL_SWEEP_SENTINEL)

    monkeypatch.setattr(diagnostics, "_sweep_owned_group", _failing_sweep)

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        diagnostics.run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert FINAL_SWEEP_SENTINEL not in str(caught.value) + repr(caught.value)
    child = _optional_pid(tmp_path, "child.json", 10.0)
    descendant = _optional_pid(tmp_path, "descendant.json", 10.0)
    extra = [pid for pid in (child, descendant) if pid is not None]
    _assert_spawned_gone(spawned, sentinel_process=sentinel_process, extra_pids=extra)
