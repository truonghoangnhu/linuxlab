"""Task 15 release-gate run identity, preflight, and environment
resolution (T15-D06, T15-D10, T15-D21).

Fail-closed: a gate run refuses to proceed against a dirty git working
tree, a non-Linux platform (checked explicitly by the CLI, not baked
into build_gate_environment(), so unit tests can inject a platform
value without needing an actual Linux host), or a `--keep-on-failure`
request made under `CI=true`.
"""

import os
import platform
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_RUN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class PreflightError(Exception):
    """Raised for any condition that must abort before a gate run even
    starts. Maps to PRECHECK_FAILURE (exit 2) at the CLI boundary."""


def generate_run_id() -> str:
    """A short, lowercase, filesystem/DB-identifier-safe run id -- safe
    to embed directly in `linuxlab_gate_central_<run_id>`-style database
    names and artifact directory names."""
    return "r" + uuid.uuid4().hex[:12]


def assert_valid_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.match(run_id):
        raise PreflightError(f"run_id {run_id!r} is not filesystem/DB-name safe")


def resolve_git_sha(repo_root=None) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def assert_clean_working_tree(repo_root=None) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip():
        raise PreflightError(
            "working tree is not clean; refusing to run the release gate against it"
        )


def assert_linux_platform(platform_name=None) -> None:
    platform_name = platform_name if platform_name is not None else platform.system()
    if platform_name != "Linux":
        raise PreflightError(
            f"release gate requires Linux; got {platform_name!r} "
            "(macOS is development-only, T15-D10)"
        )


def assert_keep_on_failure_allowed(keep_on_failure: bool, env=None) -> None:
    env = env if env is not None else os.environ
    if keep_on_failure and env.get("CI") == "true":
        raise PreflightError(
            "--keep-on-failure is a local-development-only debugging feature; "
            "rejected when CI=true (T15-D21)"
        )


@dataclass(frozen=True)
class GateRunIdentity:
    run_id: str
    git_sha: str


@dataclass(frozen=True)
class DatabaseAdminConnection:
    """Non-secret except `password`, which is excluded from repr() so
    it can never leak through an accidental log/print of the
    connection object -- only explicit attribute access reads it."""

    host: str
    port: str
    user: str
    password: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class GateEnvironment:
    run_id: str
    git_sha: str
    repo_root: Path
    artifact_dir: Path
    db_admin: DatabaseAdminConnection
    keep_on_failure: bool


def build_gate_environment(*, repo_root, keep_on_failure=False, env=None) -> GateEnvironment:
    """Resolve one gate run's identity and non-secret environment.
    Does NOT check the platform -- callers (the CLI) call
    assert_linux_platform() separately."""
    env = env if env is not None else os.environ
    assert_keep_on_failure_allowed(keep_on_failure, env=env)
    assert_clean_working_tree(repo_root)

    run_id = generate_run_id()
    assert_valid_run_id(run_id)
    git_sha = resolve_git_sha(repo_root)

    repo_root_path = Path(repo_root)
    artifact_dir = repo_root_path / "artifacts" / "phase1-release" / run_id

    db_admin = DatabaseAdminConnection(
        host=env.get("POSTGRES_HOST", "127.0.0.1"),
        port=env.get("POSTGRES_PORT", "5432"),
        user=env.get("POSTGRES_USER", "linuxlab"),
        password=env.get("POSTGRES_PASSWORD", ""),
    )

    return GateEnvironment(
        run_id=run_id,
        git_sha=git_sha,
        repo_root=repo_root_path,
        artifact_dir=artifact_dir,
        db_admin=db_admin,
        keep_on_failure=keep_on_failure,
    )
