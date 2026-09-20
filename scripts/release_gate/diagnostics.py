"""Release-gate target-process bridge for canonical RELEASE diagnostics
(Corrective B Task 13, approved spec §8; plan Task 13).

Two roles live here, on either side of one process boundary:

PARENT (the gate process, any profile):
    run_release_diagnostics(profile_env, expected_profile, repo_root,
                            fixture_key_paths=None) -> DiagnosticReport
  spawns a SUPERVISOR target process
      python -m scripts.release_gate.diagnostics --expected-profile <p>
  under the actual deployment profile environment (the outer gate's own
  Central settings are never used to reconstruct another profile's
  policy), pipes the isolated fixture key paths as one JSON object on
  stdin (an empty object when none are supplied), captures stdout/stderr
  through bounded reads, and validates the returned canonical report
  structure/profile/mode with the Task-1 core validator. Any launch
  failure, timeout, output overflow, nonzero exit, malformed report or
  overall FAIL raises the cleared Task-12 ReleaseDiagnosticsError with a
  fixed message -- never raw stdout/stderr, environment, key material or
  exception text.

TARGET (the deployment profile's own process):
    target_release_diagnostics(expected_profile, *, fixture_key_paths=None) -> int
  django.setup() under the deployment settings, builds the canonical
  release DiagnosticContext, verifies the profile, instantiates the same
  canonical providers (no checks run, no network, no writes), executes
  the real diagnostic command in a CHILD process with identical
  environment, and hands the child's stdout/return code to the cleared
  Task-12 validate_release_diagnostics(). The canonical report JSON is
  the only stdout; exit 0 for PASS/WARN, nonzero otherwise.

The child argv in a normal deployment is exactly
[sys.executable, "manage.py", "diagnose_deployment", "--mode", "release",
"--format", "json"]. Only when the harness supplies its already-created
isolated fixture key paths does the target select the cli.py
`diagnose-release` fixture child instead, which substitutes the fixed
loader path attributes for that short-lived process and then calls the
same unmodified command; no key generation or fixture setup happens in
diagnostics. Both processes inherit the actual deployment database; the
`postgres` maintenance database is never substituted.

Bounds: child timeout 60 s, supervisor timeout 120 s, stdout and stderr
each capped at 65536 bytes (overflow of either fails closed and stops the
process), concurrent draining so a pipe can never deadlock.

Process-tree containment (T13-I1, T13-C-I1, T13-C-I2): the harness owns
exactly ONE session/process group, led by the supervisor; the diagnostic
command child and everything it spawns stay in that same group. That
makes cleanup enforceable by the PARENT alone, with no supervisor
cooperation: on supervisor timeout/overflow the harness terminates the
group (SIGTERM, bounded grace, SIGKILL -- SIGKILL also removes a stopped
or wedged supervisor), and after the supervisor has exited for ANY
reason the harness sweeps the group with SIGKILL and fails closed if a
survivor existed (no detached-child use case exists for release
diagnostics, so a valid report can never buy success while an unowned
process lives on). Ownership of every spawned process begins the instant
Popen returns, inside a protected scope, so reader/setup failures cannot
abandon a live child either. Inside the supervisor, the direct child is
killed and reaped on any abnormal completion and the shared group gets a
courtesy SIGTERM (self excluded); the authoritative sweep is the parent's.
No policy is parsed from the environment here -- only the canonical
validator decides.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from linuxlab.core.diagnostics import (
    DiagnosticContext,
    DiagnosticContractError,
    DiagnosticReport,
    report_to_json,
    validate_diagnostic_report,
)
from linuxlab.core.release_diagnostics import ReleaseDiagnosticsError, validate_release_diagnostics

REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = REPO_ROOT / "scripts" / "release_gate" / "cli.py"

CHILD_TIMEOUT_SECONDS = 60
SUPERVISOR_TIMEOUT_SECONDS = 120
OUTPUT_LIMIT_BYTES = 65536
_READ_CHUNK_BYTES = 8192
_TERMINATION_GRACE_SECONDS = 5
_SWEEP_SECONDS = 5
_PROFILES = ("central", "node")

# Fixed safe messages -- the ONLY text this module ever attaches to an
# error or writes to stderr.
_MSG_BAD_PROFILE = "release diagnostics expected profile is not a deployment profile"
_MSG_BAD_FIXTURE = "release diagnostics fixture key paths are not valid for the expected profile"
_MSG_LAUNCH = "release diagnostics target process could not be launched"
_MSG_TIMEOUT = "release diagnostics target process timed out"
_MSG_OVERFLOW = "release diagnostics target process produced oversized output"
_MSG_TARGET_FAILED = "release diagnostics target process failed"
_MSG_STRAY = "release diagnostics target left stray processes"
_MSG_SUPERVISION = "release diagnostics process supervision failed"
_MSG_REPORT_INVALID = "release diagnostics target output is not a valid canonical report"
_MSG_REPORT_FAILED = "release diagnostics reported FAIL"
_MSG_PROFILE_MISMATCH = "release diagnostics target profile does not match the expected profile"
_MSG_CHILD_TIMEOUT = "release diagnostics command timed out"
_MSG_CHILD_OVERFLOW = "release diagnostics command produced oversized output"
_MSG_CHILD_LAUNCH = "release diagnostics command could not be launched"
_MSG_TARGET_ERROR = "release diagnostics target failed"


@dataclass(frozen=True)
class CommandOutcome:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    overflow: bool = False
    stray_processes: bool = False


def _drain(stream, sink: list, overflow: threading.Event) -> None:
    total = 0
    try:
        while True:
            chunk = stream.read1(_READ_CHUNK_BYTES)
            if not chunk:
                return
            total += len(chunk)
            if total > OUTPUT_LIMIT_BYTES:
                overflow.set()
                return
            sink.append(chunk)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _signal_group(pgid: int, signum: int) -> bool:
    """True when at least one process in the group existed to receive it."""
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # fail closed: something we could not signal may still exist
    return True


def _best_effort_poll_exited(process: subprocess.Popen) -> bool:
    """True only when poll() SUCCEEDS and reports the process has exited.
    A failing/lying poll() is treated as "not confirmed exited" (False),
    the SAME as "still running" -- it is never used as a gate that could
    SKIP termination; it only ever decides whether to loop again while
    polling for the ordinary success/overflow path (T13-C-I1-B)."""
    try:
        return process.poll() is not None
    except Exception:  # noqa: BLE001 -- observation failure, never a skip signal
        return False


def _reap(process: subprocess.Popen, timeout: float) -> None:
    """Best-effort wait -- ANY exception (not just the expected
    TimeoutExpired) is swallowed here: a failing/lying wait() must never
    prevent whatever cleanup step runs next (T13-C-I1-B)."""
    try:
        process.wait(timeout=timeout)
    except Exception:  # noqa: BLE001, S110 -- observation failure must never block cleanup
        pass


def _best_effort(step) -> None:
    """Run one cleanup step; ANY exception it raises is swallowed so the
    monotonic sequence of steps around it always continues to the next,
    stronger step (T13-C-I1-B: a single signal/observation failure must
    never abort escalation toward SIGKILL). Used only INSIDE a single
    termination/signal helper, where the helper's own caller does not
    need to know a step failed (the next step in that same helper is
    unconditional regardless). Finalization PHASES themselves (called
    from _run_command's finally) use `_guard` instead, which also
    reports whether the phase succeeded."""
    try:
        step()
    except Exception:  # noqa: BLE001, S110 -- see _best_effort's own docstring
        pass


def _guard(step, *, default=None):
    """Run one FINALIZATION PHASE in isolation (T13-C-I1-B, round 3): ANY
    exception it raises -- including one that a lower-level helper (e.g.
    _signal_group's own os.killpg call, or a reader's join()) did not
    itself catch -- is swallowed HERE, at the phase boundary, so it can
    never escape `_run_command`'s finally block, skip the phases that
    textually follow it in that block, or leak its raw text outward.
    Returns `(result, ok)`: `ok` is False whenever the phase raised, and
    `result` is `default` in that case -- callers that need a value
    (e.g. whether the final sweep found a survivor) pass a FAIL-CLOSED
    default, so an unobservable outcome is never treated as a clean one.
    The caller accumulates `ok` into an overall finalization-success
    flag; `_run_command` fails closed with the fixed supervision error
    if that flag is ever False, even when every process was actually
    contained -- completeness could not be PROVEN, so success is never
    reported on the strength of a swallowed exception alone."""
    try:
        return step(), True
    except Exception:  # noqa: BLE001 -- finalization must never abort here; see docstring
        return default, False


def _terminate_owned_group(process: subprocess.Popen) -> None:
    """Parent-enforced termination of the group `process` leads (it was
    started with start_new_session=True): SIGTERM, bounded grace, then
    SIGKILL for whatever remains -- including a stopped, deadlocked or
    signal-ignoring member -- then reap the direct child. Each step is
    independently best-effort (T13-C-I1-B): termination is driven by the
    already-known owned PGID, never gated on a successful poll/wait, so a
    failure in any one step cannot prevent a later, stronger one."""
    _best_effort(lambda: _signal_group(process.pid, signal.SIGTERM))
    _reap(process, _TERMINATION_GRACE_SECONDS)
    _best_effort(lambda: _signal_group(process.pid, signal.SIGKILL))
    _reap(process, 10)


def _sweep_owned_group(pgid: int) -> bool:
    """After the group leader has been reaped, SIGKILL any member still in
    the group and wait (bounded) until the group is empty. Returns True
    when a survivor existed -- the caller must then fail closed. Each
    individual signal attempt is independently guarded (T13-C-I1-B,
    round 3): a single call raising something outside _signal_group's
    own expected-error handling does not abort the loop -- it is treated
    as "cannot confirm empty yet" (fail-closed) and the bounded loop
    simply tries again on its next iteration, up to the same deadline."""

    def _kill_once():
        return _signal_group(pgid, signal.SIGKILL)

    survivors, _ok = _guard(_kill_once, default=True)
    deadline = time.monotonic() + _SWEEP_SECONDS
    remaining = survivors
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining, _ok = _guard(_kill_once, default=True)
    return survivors


def _last_resort_kill(pgid: int) -> None:
    """Absolute last-resort containment (T13-C-I1-B, round 3), reached
    only when the sweep PHASE ITSELF could not be completed: one direct,
    best-effort SIGKILL to the owned group using the lowest-level
    primitive (os.killpg), independent of _sweep_owned_group /
    _signal_group. "Subsequent OS kill operations were still usable" is
    exactly the case this exists for -- a defect in one higher-level
    helper is never the sole reason a still-signalable tree survives."""
    _best_effort(lambda: os.killpg(pgid, signal.SIGKILL))


def _terminate_direct_child(process: subprocess.Popen) -> None:
    """Inside the supervisor: the direct child is not a group leader (it
    shares the supervisor's group), so kill and reap it alone. Best
    effort: a failing kill() must not skip the reap attempt."""
    _best_effort(process.kill)
    _reap(process, 10)


def _courtesy_terminate_shared_group() -> None:
    """Inside the supervisor: ask every other member of our shared group
    (descendants of the finished child) to exit, with SIGTERM ignored for
    ourselves only for the instant it is sent. Best effort -- the parent's
    SIGKILL sweep is the authoritative cleanup, so any failure here
    (including restoring the handler) is swallowed."""
    try:
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except Exception:  # noqa: BLE001 -- best effort only
        return
    _best_effort(lambda: _signal_group(os.getpgid(0), signal.SIGTERM))
    _best_effort(lambda: signal.signal(signal.SIGTERM, previous))


def _run_command(argv, *, env, stdin_text, timeout_seconds, own_group) -> CommandOutcome:
    """Run one argv (never a shell string) with bounded, concurrently
    drained stdout/stderr and a hard timeout.

    `own_group=True` (harness -> supervisor): the child leads a new
    session/process group that the HARNESS owns end to end -- terminated
    TERM/grace/KILL on timeout/overflow and swept with SIGKILL after the
    child has exited for any reason; a survivor makes the outcome
    `stray_processes=True` so the caller fails closed.

    `own_group=False` (supervisor -> diagnostic command): the child shares
    the supervisor's group; it is killed and reaped alone on abnormal
    completion and the shared group receives a courtesy SIGTERM, leaving
    the authoritative sweep to the parent.

    Ownership begins the instant Popen returns: every later failure --
    reader creation/start, stdin, polling, an internal exception --
    passes through the same protected cleanup before returning/raising.
    """
    # T13-C-I1-A: nothing fallible runs between a successful Popen and the
    # protected scope below -- Popen itself is the ONLY statement outside
    # it, because ownership (the PID/PGID) does not exist until it returns.
    try:
        process = subprocess.Popen(  # an argv list, never a shell string
            list(argv),
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=own_group,
        )
    except (OSError, ValueError):
        raise ReleaseDiagnosticsError(_MSG_LAUNCH) from None

    out_chunks: list = []
    err_chunks: list = []
    readers: list = []
    overflow = None
    timed_out = False
    internal_failure = False
    try:
        overflow = threading.Event()
        readers = [
            threading.Thread(target=_drain, args=(process.stdout, out_chunks, overflow), daemon=True),
            threading.Thread(target=_drain, args=(process.stderr, err_chunks, overflow), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            process.stdin.write((stdin_text or "").encode("utf-8"))
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        deadline = time.monotonic() + timeout_seconds
        while not overflow.is_set():
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if _best_effort_poll_exited(process):
                break
            time.sleep(0.05)
    except Exception:  # noqa: BLE001 -- any supervision failure is contained after cleanup
        internal_failure = True
    finally:
        # Protected cleanup for EVERY exit path from here on, and it is
        # driven by OWNERSHIP (we know we spawned `process`), never by a
        # successful poll()/wait() observation (T13-C-I1-B) -- an abnormal
        # completion (timeout, overflow, or an internal_failure above)
        # always attempts full termination; a clean exit still runs the
        # SAME best-effort reap/sweep so a process that raced past our
        # poll is still contained. Group sweep / courtesy termination
        # runs BEFORE the reader joins: a descendant that inherited the
        # child's pipe ends keeps a reader blocked in read1() until it
        # dies, and the main thread must never close a stream a reader is
        # blocked on (BufferedReader lock).
        forced = timed_out or internal_failure or (overflow is not None and overflow.is_set())
        # Every phase below is independently guarded (T13-C-I1-B, round 3):
        # a phase that raises is recorded in `finalization_ok` but never
        # prevents a LATER phase from still running -- termination, reap,
        # sweep and every reader join are each attempted regardless of
        # what an earlier phase did.
        finalization_ok = True

        if forced or not _best_effort_poll_exited(process):
            if own_group:
                _, ok = _guard(lambda: _terminate_owned_group(process))
            else:
                _, ok = _guard(lambda: _terminate_direct_child(process))
            finalization_ok = finalization_ok and ok

        _, ok = _guard(lambda: _reap(process, 10))
        finalization_ok = finalization_ok and ok

        if own_group:
            # Fail-closed default: if the sweep itself could not be
            # completed, a survivor is ASSUMED -- never assumed absent.
            stray, ok = _guard(lambda: _sweep_owned_group(process.pid), default=True)
            if not ok:
                # The sweep PHASE failed (not merely one internal signal
                # attempt, which _sweep_owned_group already tolerates) --
                # fall back to the independent last-resort primitive
                # rather than leaving containment to that one helper alone.
                _guard(lambda: _last_resort_kill(process.pid))
            finalization_ok = finalization_ok and ok
        else:
            _, ok = _guard(_courtesy_terminate_shared_group)
            finalization_ok = finalization_ok and ok
            stray = False

        for reader in readers:
            _, ok = _guard(
                lambda reader=reader: reader.join(timeout=2) if reader.ident is not None else None
            )
            finalization_ok = finalization_ok and ok

    if internal_failure or not finalization_ok:
        # Either supervision itself failed, or at least one finalization
        # phase raised and containment could therefore not be PROVEN --
        # the owned tree has still been cleaned up on a best-effort basis
        # above, but a report is never returned as successful on the
        # strength of a swallowed exception; surface only the fixed
        # message, never any internal exception text.
        raise ReleaseDiagnosticsError(_MSG_SUPERVISION)
    if overflow is not None and overflow.is_set():
        return CommandOutcome(process.returncode, b"", b"", overflow=True, stray_processes=stray)
    if timed_out:
        return CommandOutcome(process.returncode, b"", b"", timed_out=True, stray_processes=stray)
    return CommandOutcome(
        process.returncode, b"".join(out_chunks), b"".join(err_chunks), stray_processes=stray
    )


def _validate_fixture_paths(expected_profile, fixture_key_paths):
    """(Central signing path, Node trust path): only the element matching
    the expected profile may be populated. Returns the stdin payload."""
    if fixture_key_paths is None:
        return {}
    if not isinstance(fixture_key_paths, tuple) or len(fixture_key_paths) != 2:
        raise ReleaseDiagnosticsError(_MSG_BAD_FIXTURE)
    signing_path, trust_path = fixture_key_paths
    for value in (signing_path, trust_path):
        if value is not None and not isinstance(value, (str, Path)):
            raise ReleaseDiagnosticsError(_MSG_BAD_FIXTURE)
    if expected_profile == "central" and trust_path is not None:
        raise ReleaseDiagnosticsError(_MSG_BAD_FIXTURE)
    if expected_profile == "node" and signing_path is not None:
        raise ReleaseDiagnosticsError(_MSG_BAD_FIXTURE)
    payload = {}
    if signing_path is not None:
        payload["signing_key_path"] = str(signing_path)
    if trust_path is not None:
        payload["trust_anchor_path"] = str(trust_path)
    return payload


def _supervisor_command() -> list[str]:
    return [sys.executable, "-m", "scripts.release_gate.diagnostics"]


def run_release_diagnostics(
    profile_env, *, expected_profile, repo_root, fixture_key_paths=None
) -> DiagnosticReport:
    if expected_profile not in _PROFILES:
        raise ReleaseDiagnosticsError(_MSG_BAD_PROFILE)
    payload = _validate_fixture_paths(expected_profile, fixture_key_paths)
    del repo_root  # the bridge always runs inside this repository's own root
    env = {**os.environ, **dict(profile_env)}
    argv = [*_supervisor_command(), "--expected-profile", expected_profile]
    outcome = _run_command(
        argv,
        env=env,
        stdin_text=json.dumps(payload),
        timeout_seconds=SUPERVISOR_TIMEOUT_SECONDS,
        own_group=True,
    )
    if outcome.timed_out:
        raise ReleaseDiagnosticsError(_MSG_TIMEOUT)
    if outcome.overflow:
        raise ReleaseDiagnosticsError(_MSG_OVERFLOW)
    if outcome.returncode != 0:
        raise ReleaseDiagnosticsError(_MSG_TARGET_FAILED)
    if outcome.stray_processes:
        raise ReleaseDiagnosticsError(_MSG_STRAY)
    try:
        text = outcome.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise ReleaseDiagnosticsError(_MSG_REPORT_INVALID) from None
    context = DiagnosticContext(expected_profile, "release", frozenset())
    try:
        report = validate_diagnostic_report(text, context=context, required_codes=frozenset())
    except DiagnosticContractError:
        raise ReleaseDiagnosticsError(_MSG_REPORT_INVALID) from None
    if report.overall_status == "FAIL":
        raise ReleaseDiagnosticsError(_MSG_REPORT_FAILED)
    return report


def _child_invocation(fixture_key_paths):
    """The real diagnostic command in a normal deployment; the cli.py
    fixture child only when the harness supplied isolated key paths."""
    payload = _validate_fixture_paths_for_target(fixture_key_paths)
    if payload is None:
        return (
            [sys.executable, "manage.py", "diagnose_deployment", "--mode", "release", "--format", "json"],
            "",
        )
    return [sys.executable, str(_CLI_PATH), "diagnose-release"], json.dumps(payload)


def _validate_fixture_paths_for_target(fixture_key_paths):
    if fixture_key_paths is None:
        return None
    signing_path, trust_path = fixture_key_paths
    if signing_path is None and trust_path is None:
        return None
    payload = {}
    if signing_path is not None:
        payload["signing_key_path"] = str(signing_path)
    if trust_path is not None:
        payload["trust_anchor_path"] = str(trust_path)
    return payload


def target_release_diagnostics(expected_profile, *, fixture_key_paths=None) -> int:
    """Target-process entry: exit 0 only for a canonical PASS/WARN release
    report, which is the sole stdout content; nonzero with a fixed safe
    stderr line for every other outcome."""
    try:
        if expected_profile not in _PROFILES:
            raise ReleaseDiagnosticsError(_MSG_BAD_PROFILE)
        import django

        django.setup()
        from linuxlab.core.diagnostics import build_diagnostic_context, diagnostic_providers

        context = build_diagnostic_context("release")
        if context.profile != expected_profile:
            raise ReleaseDiagnosticsError(_MSG_PROFILE_MISMATCH)
        providers = diagnostic_providers()
        argv, stdin_text = _child_invocation(fixture_key_paths)
        outcome = _run_command(
            argv,
            env=dict(os.environ),
            stdin_text=stdin_text,
            timeout_seconds=CHILD_TIMEOUT_SECONDS,
            own_group=False,
        )
        if outcome.timed_out:
            raise ReleaseDiagnosticsError(_MSG_CHILD_TIMEOUT)
        if outcome.overflow:
            raise ReleaseDiagnosticsError(_MSG_CHILD_OVERFLOW)
        try:
            stdout_text = outcome.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise ReleaseDiagnosticsError(_MSG_REPORT_INVALID) from None
        report = validate_release_diagnostics(
            stdout_text, outcome.returncode, context=context, providers=providers
        )
    except ReleaseDiagnosticsError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    except DiagnosticContractError:
        sys.stderr.write(_MSG_REPORT_INVALID + "\n")
        return 1
    except Exception:  # noqa: BLE001 -- never a traceback/raw text to the parent
        sys.stderr.write(_MSG_TARGET_ERROR + "\n")
        return 1
    sys.stdout.write(report_to_json(report))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def _read_fixture_paths_from_stdin():
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict):
        raise ReleaseDiagnosticsError(_MSG_BAD_FIXTURE)
    signing = payload.get("signing_key_path")
    trust = payload.get("trust_anchor_path")
    for value in (signing, trust):
        if value is not None and not isinstance(value, str):
            raise ReleaseDiagnosticsError(_MSG_BAD_FIXTURE)
    if signing is None and trust is None:
        return None
    return (Path(signing) if signing else None, Path(trust) if trust else None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--expected-profile", required=True, choices=_PROFILES)
    args = parser.parse_args(argv)
    try:
        fixture_key_paths = _read_fixture_paths_from_stdin()
    except (ReleaseDiagnosticsError, ValueError):
        sys.stderr.write(_MSG_BAD_FIXTURE + "\n")
        return 1
    return target_release_diagnostics(args.expected_profile, fixture_key_paths=fixture_key_paths)


if __name__ == "__main__":
    sys.exit(main())
