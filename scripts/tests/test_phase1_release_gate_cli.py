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

import pytest

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


def test_cli_prints_only_sanitized_terminal_failure_fields(monkeypatch, capsys):
    """Task 15 Task 11 corrective v6, Section 5/15: when run_release_gate()
    returns the last-resort terminal fail-safe result (evidence=None,
    terminal_failure populated), the shipped CLI must still print only
    the already-sanitized category/stage/code/safe_message fields --
    never a raw exception message, `str(exc)`/`repr(exc)`, or a
    traceback -- and must never crash trying to read evidence.* fields
    that do not exist on this result."""
    import scripts.release_gate.gate_runner as gate_runner_module
    from scripts.phase1_release_gate import main

    sentinel = "sentinel-should-never-reach-cli-output-7d2a"
    fake_result = gate_runner_module.GateRunResult(
        overall="CLEANUP_FAILURE",
        exit_code=5,
        evidence=None,
        evidence_dir=None,
        json_path=None,
        markdown_path=None,
        terminal_failure={
            "category": "CLEANUP_FAILURE",
            "stage": "finalize",
            "code": "RuntimeError",
            "safe_message": "RuntimeError raised during finalize",
        },
    )
    monkeypatch.setattr(gate_runner_module, "run_release_gate", lambda **kwargs: fake_result)

    exit_code = main([])

    assert exit_code == 5
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "terminal failure: CLEANUP_FAILURE at finalize (RuntimeError)" in output
    assert sentinel not in output
    assert "Traceback" not in output


def test_cli_final_containment_when_run_release_gate_itself_raises(monkeypatch, capsys):
    """Task 15 Task 11 corrective v7, Section 9: run_release_gate() is
    designed to never raise (it has its own outer terminal fail-safe),
    but the shipped CLI provides ONE MORE fixed-literal safety boundary
    beyond that, in case it somehow does anyway. Must print only a fixed
    safe message -- never `str(exc)`/`repr(exc)`/a traceback/any sentinel
    the raised exception happened to carry -- and return a deterministic
    non-zero exit code."""
    import scripts.release_gate.gate_runner as gate_runner_module
    from scripts.phase1_release_gate import main

    sentinel = "sentinel-should-never-reach-cli-output-9f1c"

    def _raises(**kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(gate_runner_module, "run_release_gate", _raises)

    exit_code = main([])

    assert exit_code == 5
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert sentinel not in output
    assert "Traceback" not in output
    assert "RuntimeError" not in output


# --- Corrective B Task 13: no CLI switch can bypass or weaken canonical
# release diagnostics (spec §5/§7: "No manual flag can disable this
# production requirement"). ------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        "--skip-diagnostics",
        "--no-diagnostics",
        "--require-central-probe=false",
        "--skip-check=TRUST_KEY",
        "--no-remote",
    ],
)
def test_cli_rejects_release_diagnostics_override_flags(flag):
    result = subprocess.run(
        [str(PYTHON), str(CLI_PATH), flag],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
