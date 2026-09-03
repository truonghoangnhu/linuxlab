"""Task 15 Tasks 7-10 corrective, Critical 2: sensitive subprocess
inputs (Node bearer secret, correct-answer/grading material) must
never cross the scripts/release_gate/cli.py process boundary via argv.
They travel exclusively as JSON on the child's stdin. Proven with a
real subprocess, real live-argv inspection via `ps` while the child is
still alive, and real stdout/stderr/ActionError text after it exits --
never a simulation of any of those channels.
"""

import json
import os
import re
import subprocess
import time

import pytest

from scripts.release_gate import e2e_support, http_bridge
from scripts.release_gate.e2e_support import (
    author_and_package,
    dual_deployment,
    import_and_activate,
    run_action,
)


def _poll_live_argv_until_exit(popen):
    """Repeatedly samples the live child's own command line via `ps`
    (portable across the macOS dev host and Linux CI) until it exits.
    Every returned snapshot is a genuine observation of that process's
    real argv while `popen.poll() is None` confirmed it was still
    alive -- never a simulated/predicted value."""
    snapshots = []
    while popen.poll() is None:
        result = subprocess.run(
            ["ps", "-o", "args=", "-p", str(popen.pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            snapshots.append(result.stdout)
        time.sleep(0.002)
    return snapshots


def _run_cli_with_stdin(argv_tail, profile_env, stdin_payload):
    """Invokes the real scripts/release_gate/cli.py subprocess directly
    (bypassing run_action()) so the test can observe its live argv and
    control its stdin precisely."""
    argv = [str(e2e_support.PYTHON), str(e2e_support.CLI_PATH), *argv_tail]
    popen = subprocess.Popen(
        argv,
        cwd=str(e2e_support.REPO_ROOT),
        env={**os.environ, **profile_env},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    popen.stdin.write(json.dumps(stdin_payload).encode())
    popen.stdin.close()
    popen.stdin = None  # already closed above -- stop communicate() from re-flushing it
    snapshots = _poll_live_argv_until_exit(popen)
    stdout, stderr = popen.communicate(timeout=30)
    return popen.returncode, stdout.decode(), stderr.decode(), snapshots


def test_answer_key_flag_never_exists_on_argv_while_child_runs(tmp_path):
    """`correct_option_id` is the answer/grading fact -- WHICH already-
    visible option id is correct -- so its own string value legitimately
    also appears in `--options-json` (the non-secret question content
    every learner sees). The real, checkable invariant is that the
    `--correct-option-id` FLAG itself no longer exists on this
    subcommand's argv at all -- proven both statically (argparse rejects
    it) and dynamically (a real live child's own argv, sampled by `ps`
    while it is still running, never contains it)."""
    with dual_deployment(tmp_path, start_node=False) as deployment:
        argv_tail = [
            "author-and-publish",
            "--prompt",
            "Sentinel prompt",
            "--options-json",
            json.dumps([{"id": "opt-x", "text": "opt-x text"}, {"id": "opt-y", "text": "opt-y text"}]),
            "--max-score",
            "1",
        ]
        returncode, stdout, stderr, snapshots = _run_cli_with_stdin(
            argv_tail, deployment.central_profile_env, {"correct_option_id": "opt-y"}
        )

        assert returncode == 0, stdout + stderr
        assert snapshots, "expected at least one live-argv snapshot while the child ran"
        for snapshot in snapshots:
            assert "--correct-option-id" not in snapshot
        assert "--correct-option-id" not in stdout
        assert "--correct-option-id" not in stderr

        central_log = deployment.central.log_path
        if central_log is not None and central_log.exists():
            assert "--correct-option-id" not in central_log.read_text(errors="replace")


def test_author_and_publish_rejects_correct_option_id_as_an_argv_flag():
    """Static proof alongside the dynamic one above: argparse itself no
    longer recognizes `--correct-option-id` on this subcommand."""
    from scripts.release_gate.cli import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "author-and-publish",
                "--prompt",
                "x",
                "--options-json",
                "[]",
                "--correct-option-id",
                "a",
            ]
        )


def _submit_and_get_pending_event_id(deployment):
    authored, package_path = author_and_package(
        deployment,
        prompt="Which command lists files?",
        options=[{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
        correct_option_id="a",
    )
    import_and_activate(deployment, package_path)
    session = http_bridge.LearnerSession.start(deployment.node.base_url)
    http_bridge.get_landing(
        session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
    )
    _status, attempt_url_path = http_bridge.post_start(
        session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
    )
    _status, attempt_body = http_bridge.get_attempt(session, attempt_url_path)
    question_public_id = re.search(r'data-question-public-id="([0-9a-f-]{36})"', attempt_body).group(1)
    status, submit_body = http_bridge.post_submit(
        session,
        attempt_url_path,
        [{"question_public_id": question_public_id, "question_version": authored["question_version"], "selected_option_id": "a"}],
    )
    assert status == 200
    outbox = run_action(
        deployment.node_profile_env,
        "find-outbox-message-by-attempt",
        "--attempt-public-id",
        submit_body["attempt_public_id"],
    )
    return outbox["event_id"]


def test_node_secret_never_appears_in_argv_stdout_stderr_or_logs(tmp_path):
    """Uses the REAL issued `deployment.node_secret` (not an arbitrary
    placeholder) so this exercises the genuine successful resend path --
    the same one N3 depends on -- while proving that value never once
    touches argv, stdout, stderr, or either Gunicorn's log."""
    with dual_deployment(tmp_path) as deployment:
        event_id = _submit_and_get_pending_event_id(deployment)
        secret = deployment.node_secret

        argv_tail = [
            "resend-outbox-message",
            "--event-id",
            event_id,
            "--central-url",
            deployment.central.base_url,
            "--node-id",
            deployment.node_id,
        ]
        returncode, stdout, stderr, snapshots = _run_cli_with_stdin(
            argv_tail, deployment.node_profile_env, {"secret": secret}
        )

        assert returncode == 0, stdout + stderr
        assert snapshots, "expected at least one live-argv snapshot while the child ran"
        for snapshot in snapshots:
            assert secret not in snapshot
            assert "--secret" not in snapshot
        assert secret not in stdout
        assert secret not in stderr

        for log_path in (deployment.central.log_path, deployment.node.log_path):
            if log_path is not None and log_path.exists():
                assert secret not in log_path.read_text(errors="replace")


def test_resend_outbox_message_rejects_secret_as_an_argv_flag():
    """Static proof alongside the dynamic one above: argparse itself no
    longer recognizes `--secret` on this subcommand."""
    from scripts.release_gate.cli import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "resend-outbox-message",
                "--event-id",
                "x",
                "--central-url",
                "http://example.invalid",
                "--node-id",
                "n",
                "--secret",
                "s",
            ]
        )


def test_run_action_error_on_bad_action_name_omits_raw_argv_repr(tmp_path):
    """ActionError text (as raised by e2e_support.run_action(), the same
    path every Task 8/9 scenario uses) must be built from a safe action
    name + exit code + sanitized stdout/stderr -- never a repr of the
    full argv tuple passed to subprocess.run() -- so a sensitive value
    could never reach it even by accident. An unrecognized subcommand
    makes argparse itself fail before any JSON is printed, exercising
    run_action()'s non-JSON-output error-formatting branch directly."""
    with dual_deployment(tmp_path, start_node=False) as deployment:
        with pytest.raises(e2e_support.ActionError) as excinfo:
            run_action(deployment.central_profile_env, "not-a-real-action")
        message = str(excinfo.value)
        assert "not-a-real-action" in message
        assert "cli.py (" not in message
        assert "'--" not in message
