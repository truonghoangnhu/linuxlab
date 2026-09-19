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
def _canned_run(argv, *, env, stdin_text, timeout_seconds, new_process_group=False):
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

    def _capture(argv, *, env, stdin_text, timeout_seconds, new_process_group=False):
        captured.update(argv=list(argv), stdin=stdin_text, timeout=timeout_seconds, group=new_process_group)
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


# --- Corrective (T13-I1): the whole process tree owned by the inner
# diagnostic execution must be gone when the bridge returns failure -------

# A supervisor stand-in that runs the REAL target (real django.setup, real
# _run_command with its real timeout/overflow/kill logic) but points the
# inner diagnostic child at a script that spawns a sleeping DESCENDANT,
# records its PID, and then misbehaves (sleeps past the timeout, or floods
# stdout). Only the child argv and the inner timeout are replaced.
_DESCENDANT_TARGET_SCRIPT = """
import json, os, sys
from scripts.release_gate import diagnostics
mode, pid_dir = sys.argv[1], sys.argv[2]
child_script = '''
import os, subprocess, sys, time
pid_dir, mode = sys.argv[1], sys.argv[2]
descendant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(os.path.join(pid_dir, "descendant.pid"), "w").write(str(descendant.pid))
open(os.path.join(pid_dir, "child.pid"), "w").write(str(os.getpid()))
if mode == "overflow":
    sys.stdout.write("x" * 70000)
    sys.stdout.flush()
elif mode == "stderr-overflow":
    sys.stderr.write("y" * 70000)
    sys.stderr.flush()
elif mode == "nonzero":
    descendant.kill(); descendant.wait()
    sys.exit(3)
time.sleep(600)
'''
diagnostics._child_invocation = lambda paths: ([sys.executable, "-c", child_script, pid_dir, mode], "")
diagnostics.CHILD_TIMEOUT_SECONDS = 2
sys.exit(diagnostics.main(sys.argv[3:]))
"""


def _descendant_supervisor(monkeypatch, mode, pid_dir):
    from scripts.release_gate import diagnostics

    monkeypatch.setattr(
        diagnostics,
        "_supervisor_command",
        lambda: [sys.executable, "-c", _DESCENDANT_TARGET_SCRIPT, mode, str(pid_dir)],
    )


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Our own killed-but-unreaped child answers kill(0); reap it if it is ours.
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    return True


def _pid(pid_dir, name, timeout=10.0):
    deadline = time.monotonic() + timeout
    path = pid_dir / name
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{name} never written")


@pytest.mark.parametrize("mode", ["timeout", "overflow", "stderr-overflow"])
def test_inner_failure_kills_every_owned_descendant(node_target, monkeypatch, tmp_path, mode):
    from scripts.release_gate.diagnostics import run_release_diagnostics

    _descendant_supervisor(monkeypatch, mode, tmp_path)
    started = time.monotonic()

    with pytest.raises(ReleaseDiagnosticsError) as caught:
        run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)

    assert str(caught.value) == "release diagnostics target process failed"
    assert "xxx" not in str(caught.value) and "yyy" not in str(caught.value)
    assert time.monotonic() - started < 60  # the inner 2 s timeout/overflow path, not the supervisor's
    descendant_pid = _pid(tmp_path, "descendant.pid")
    child_pid = _pid(tmp_path, "child.pid")
    assert _wait_gone(child_pid)
    assert _wait_gone(descendant_pid, timeout=5.0), f"descendant {descendant_pid} survived the {mode} failure"


def test_ordinary_nonzero_child_exit_leaves_no_stray_descendant(node_target, monkeypatch, tmp_path):
    from scripts.release_gate.diagnostics import run_release_diagnostics

    _descendant_supervisor(monkeypatch, "nonzero", tmp_path)
    with pytest.raises(ReleaseDiagnosticsError):
        run_release_diagnostics(node_target.env, expected_profile="node", repo_root=REPO_ROOT)
    assert _wait_gone(_pid(tmp_path, "descendant.pid"), timeout=5.0)


def test_successful_real_command_is_not_killed_prematurely(node_target):
    """The real chain with the real command: no false timeout/overflow
    kill, canonical WARN report still returned (Task-13 D)."""
    from scripts.release_gate.diagnostics import run_release_diagnostics

    report = run_release_diagnostics(
        node_target.env, expected_profile="node", repo_root=REPO_ROOT,
        fixture_key_paths=(None, node_target.trust_path),
    )
    assert report.overall_status == "WARN"
