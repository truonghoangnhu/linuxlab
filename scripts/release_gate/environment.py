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

# Task 15 Task 11 corrective v9, Section 26: `\Z` (true end of string),
# never a permissive trailing `$` -- Python's `$` also matches
# immediately BEFORE a single trailing "\n", so a `$`-anchored pattern
# would wrongly accept a well-formed run_id followed by a trailing
# newline as "whole string valid" (the exact live bug this corrective
# closes: `assert_valid_run_id("rvalid\n")` did not raise before this
# change). Paired with `.fullmatch()` (not `.match()`) at every call
# site below for genuine whole-string semantics.
_RUN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\Z")
# Task 15 Task 11 corrective v8, Section 24: the exact canonical Git SHA
# shape this codebase's own resolve_git_sha() ("git rev-parse HEAD",
# stripped) always produces -- `\Z`, never `$`, since Python's `$` also
# matches immediately before a trailing "\n".
_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}\Z")

# Checkpoint corrective 3 (finding #1): PostgreSQL silently truncates any
# identifier longer than this to fit -- two distinct run IDs that only
# differ after the truncation point would then collide onto the SAME
# actual database. NAMEDATALEN in PostgreSQL is 64 (one byte reserved
# for the trailing NUL), so 63 is the actual usable identifier length.
POSTGRES_IDENTIFIER_MAX_BYTES = 63

# The longest prefix any canonical gate database name is built from
# (scripts/release_gate/database.py: `f"linuxlab_gate_{role}_{run_id}"`
# for role="restore_central"). Every accepted run_id must leave enough
# room under this, the worst case, so that NO role's canonical name --
# central/node/restore_central/restore_node -- can ever be truncated.
LONGEST_GATE_DB_PREFIX = "linuxlab_gate_restore_central_"

MAX_GATE_RUN_ID_BYTES = POSTGRES_IDENTIFIER_MAX_BYTES - len(
    LONGEST_GATE_DB_PREFIX.encode("ascii")
)


class PreflightError(Exception):
    """Raised for any condition that must abort before a gate run even
    starts. Maps to PRECHECK_FAILURE (exit 2) at the CLI boundary."""


def generate_run_id() -> str:
    """A short, lowercase, filesystem/DB-identifier-safe run id -- safe
    to embed directly in `linuxlab_gate_central_<run_id>`-style database
    names and artifact directory names. 13 bytes, well within
    MAX_GATE_RUN_ID_BYTES."""
    return "r" + uuid.uuid4().hex[:12]


def validate_canonical_run_id(value) -> bool:
    """Task 15 Task 11 corrective v9, Section 24: the ONE pure canonical
    run-ID grammar check, used independently at BOTH the builder
    boundary (assert_valid_run_id() below, a thin wrapper around this)
    and the final-validator boundary
    (evidence._validate_for_pass_metadata()) -- so a hand-constructed
    EvidenceV1 candidate that bypasses the builder entirely is still
    caught. Returns True/False, never raises; a `str` SUBCLASS is
    rejected via `type(x) is str` (never `isinstance`), since a
    subclass could carry hidden/overridden behavior while still
    comparing/matching equal to a well-formed run_id. Does not invent a
    new maximum length -- reuses MAX_GATE_RUN_ID_BYTES exactly as
    assert_valid_run_id() always has."""
    if type(value) is not str or _RUN_ID_PATTERN.fullmatch(value) is None:
        return False
    return len(value.encode("ascii")) <= MAX_GATE_RUN_ID_BYTES


def assert_valid_run_id(run_id: str) -> None:
    if validate_canonical_run_id(run_id):
        return
    # Task 15 Task 11 corrective v8, Section 24: `type(x) is str`, never
    # `isinstance` -- a str SUBCLASS could carry hidden/overridden
    # behavior while still comparing/matching equal to a well-formed
    # run_id; exact type matching rejects it regardless.
    if type(run_id) is not str or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise PreflightError(f"run_id {run_id!r} is not filesystem/DB-name safe")
    byte_length = len(run_id.encode("ascii"))
    raise PreflightError(
        f"run_id {run_id!r} is {byte_length} bytes, exceeding the "
        f"{MAX_GATE_RUN_ID_BYTES}-byte limit derived from PostgreSQL's "
        f"{POSTGRES_IDENTIFIER_MAX_BYTES}-byte identifier limit and the "
        f"longest gate database name prefix ({LONGEST_GATE_DB_PREFIX!r}) -- "
        "a longer run_id risks silent truncation and collision with a "
        "different run"
    )


def assert_valid_git_sha(git_sha) -> None:
    """Task 15 Task 11 corrective v8, Section 24/25: the builder-level
    boundary for the exact canonical Git SHA shape this codebase's own
    resolve_git_sha() always produces -- exactly 40 lowercase hex
    characters, `type(x) is str` (never `isinstance`, so a str subclass
    is rejected too). Builder strictness here does not replace the
    final, independent evidence.validate_for_pass() re-check (Section
    25) -- both exist."""
    if type(git_sha) is not str or not _GIT_SHA_PATTERN.match(git_sha):
        raise PreflightError("git_sha is not a valid 40-character lowercase hex SHA")


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
class Authority:
    """Task 15 Task 11 corrective v2, Section 10: functional success and
    release authority are distinct. `authoritative` is true only for a
    genuinely supported Linux run; a macOS development run may still
    PASS functionally (via `run_release_gate(check_platform=False)`)
    but its evidence must always self-identify as non-authoritative, so
    it can never be mistaken for a real release gate result."""

    authoritative: bool
    platform: str
    reason: str | None

    def __post_init__(self) -> None:
        # Task 15 Task 11 corrective v14, Section 5.4: Authority directly
        # participates in constructing the terminal `run["authority"]`
        # mapping, so it belongs to the authority-producing chain and is
        # sealed the same way every other terminal/authority dataclass
        # is -- exact builtin types only, never `isinstance()` (which a
        # scalar subclass or an object with an overridden `__class__`
        # property could satisfy while still carrying hidden/overridden
        # behavior), and never coerced with `bool()`/`str()`.
        if type(self.authoritative) is not bool:
            raise PreflightError("Authority.authoritative must be an exact bool")
        if type(self.platform) is not str:
            raise PreflightError("Authority.platform must be an exact str")
        if self.reason is not None and type(self.reason) is not str:
            raise PreflightError("Authority.reason must be an exact str or None")


def compute_authority(*, platform_name: str) -> Authority:
    is_linux = platform_name == "Linux"
    return Authority(
        authoritative=is_linux,
        platform=platform_name.lower(),
        reason=None if is_linux else "development_only_platform",
    )


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
    assert_valid_git_sha(git_sha)

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
