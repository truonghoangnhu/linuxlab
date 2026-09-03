#!/usr/bin/env python
"""Task 15 Task 11: Phase 1 release-gate CLI entrypoint.

    .venv/bin/python scripts/phase1_release_gate.py [--keep-on-failure]

Runs the full local Phase 1 release gate: preflight -> the real
dual-deployment happy path -> exactly five real negative scenarios ->
a real backup/restore smoke -> evidence assembly -> cleanup -> exit
code. Every scenario runs against a real ephemeral Central/Node
PostgreSQL database pair, real Gunicorn processes, real HTTP, the
locked production assessment/single_choice provider, a real signed
`.llpkg`, the real HttpSyncTransport, and real pg_dump/pg_restore.

All business/scenario logic lives in scripts/release_gate/scenarios.py
and the orchestration (preflight, exit-code mapping, evidence
assembly, cleanup) lives in scripts/release_gate/gate_runner.py -- this
file is deliberately a thin argv-parsing/console-output layer only, so
it contains no LinuxLab domain logic and no gate orchestration logic of
its own.

Exit codes (scripts.release_gate.evidence.EXIT_CODE, approved Task 15
design spec Section 18 "Failure Model"):

    0 = PASS
    2 = PRECHECK_FAILURE   (environment/repo/dependency/db-safety/config)
    3 = STARTUP_FAILURE    (db migration, Gunicorn startup, readiness)
    4 = SCENARIO_FAILURE   (happy-path/negative/recovery/invariant failure)
    5 = CLEANUP_FAILURE    (required cleanup cannot be completed)

Release evidence (JSON + Markdown, schema_version 1) is written to
artifacts/phase1-release/<run_id>/ regardless of PASS or FAIL -- never
secrets, only sanitized diagnostic text.

This script never creates a git tag, commit, or push. A release
candidate or final tag remains a human-controlled action outside
Task 11's scope (Tasks 12-15).
"""

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _setup_django() -> None:
    """This orchestrator process never touches a Django database itself
    -- every real ORM/DB action happens in a subprocess with its own
    profile-scoped DJANGO_SETTINGS_MODULE (scripts/release_gate/cli.py,
    `manage.py sync_events`), exactly like scripts.release_gate.
    e2e_support already does. But scripts/release_gate/scenarios.py
    imports pure-Python constants (e.g. SIGNATURE_MEMBER) from modules
    that transitively import Django models, so the app registry must be
    ready for those imports to succeed at all -- any profile settings
    module works equally well for this, since no query is ever issued
    against it from this process."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linuxlab.settings.central")
    import django

    django.setup()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phase1_release_gate.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--keep-on-failure",
        action="store_true",
        help=(
            "Local-development-only debugging aid: on a FAILED scenario, "
            "keep that scenario's ephemeral Central/Node databases and "
            "workspace directory (the .llpkg, logs, etc.) instead of "
            "dropping/removing them, for inspection. Never applies to a "
            "successful scenario. Never permits retaining the ephemeral "
            "signing private key or any plaintext credential -- those are "
            "still deleted unconditionally. Rejected when CI=true."
        ),
    )
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    _setup_django()
    from scripts.release_gate.gate_runner import run_release_gate

    result = run_release_gate(repo_root=_REPO_ROOT, keep_on_failure=args.keep_on_failure)

    print(f"phase1_release_gate: {result.overall} (exit {result.exit_code})")
    if result.evidence is not None:
        print(f"run_id: {result.evidence.run_id}")
        print(f"git_sha: {result.evidence.git_sha}")
    if result.json_path is not None:
        print(f"evidence (json): {result.json_path}")
    if result.markdown_path is not None:
        print(f"evidence (markdown): {result.markdown_path}")
    if result.evidence is not None and result.evidence.cleanup_failure:
        print(f"cleanup issue: {result.evidence.cleanup_failure}", file=sys.stderr)

    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
