"""Task 15 Task 11: scripts/phase1_release_gate.py CLI-level tests --
real subprocess invocations of the actual entrypoint, proving argv
parsing and the safe/cheap-to-trigger preflight-refusal wiring for
real. The exit-code MAPPING logic itself (PASS/PRECHECK/STARTUP/
SCENARIO/CLEANUP -> 0/2/3/4/5) and failure-precedence rules are proven
exhaustively with fast fakes in test_release_gate_gate_runner.py, since
the CLI itself contains no orchestration logic of its own -- it only
parses argv and calls scripts.release_gate.gate_runner.run_release_gate().

A real full successful gate run (all seven real scenarios, ~3-5
minutes) is exercised manually per the Task 11 verification checklist
rather than as an automated test here, to keep this suite's own
runtime reasonable; scripts/tests/test_release_gate_e2e_happy_path.py,
test_release_gate_negative_scenarios.py, and
test_release_gate_backup_restore.py already prove every one of those
seven scenarios independently and in full.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "phase1_release_gate.py"
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def test_cli_never_exposes_a_platform_check_bypass():
    """Task 11 corrective Section 13: the shipped CLI stays Linux-
    authoritative -- `check_platform=False` is a test/dev API
    parameter on run_release_gate() only, never reachable through
    argv. Structural proof: the CLI's own source never mentions it and
    its argument parser has no such option."""
    source = CLI_PATH.read_text()
    assert "check_platform" not in source

    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from scripts.phase1_release_gate import _build_parser

    parser = _build_parser()
    option_strings = {opt for action in parser._actions for opt in action.option_strings}
    assert not any("platform" in opt for opt in option_strings)


def test_cli_help_exits_0():
    result = subprocess.run(
        [str(PYTHON), str(CLI_PATH), "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "--keep-on-failure" in result.stdout


def test_cli_rejects_unknown_argument():
    result = subprocess.run(
        [str(PYTHON), str(CLI_PATH), "--not-a-real-flag"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0  # argparse's own usage-error exit, distinct from the gate's own codes


def test_cli_rejects_dirty_working_tree_with_exit_2_and_no_git_side_effects(tmp_path):
    """Real preflight refusal: a genuinely dirty working tree (an
    untracked scratch file) must make the CLI refuse with exit 2,
    before any ephemeral database/workspace/evidence is ever created --
    and must never create a git tag, commit, or push as a side effect."""
    scratch_file = REPO_ROOT / ".phase1_release_gate_cli_test_scratch_file.tmp"
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=True
    ).stdout.strip()
    before_tags = subprocess.run(
        ["git", "tag", "--list"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=True
    ).stdout

    try:
        scratch_file.write_text("dirty tree probe for phase1_release_gate.py preflight refusal\n")

        result = subprocess.run(
            [str(PYTHON), str(CLI_PATH)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        scratch_file.unlink(missing_ok=True)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "PRECHECK_FAILURE" in result.stdout

    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=True
    ).stdout.strip()
    after_tags = subprocess.run(
        ["git", "tag", "--list"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=True
    ).stdout
    assert after_head == before_head, "the CLI must never create a commit"
    assert after_tags == before_tags, "the CLI must never create a tag"
