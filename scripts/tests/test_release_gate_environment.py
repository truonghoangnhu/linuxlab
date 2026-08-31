"""Task 15 release-gate run identity/preflight (T15-D06, T15-D10, T15-D21).

Pure-Python, database-free: these are the fail-closed checks that must
run before anything else in a gate run (dirty tree, wrong platform,
unsafe --keep-on-failure) plus the run identity used to derive every
ephemeral resource name later.
"""

import subprocess

import pytest

from scripts.release_gate.environment import (
    LONGEST_GATE_DB_PREFIX,
    MAX_GATE_RUN_ID_BYTES,
    POSTGRES_IDENTIFIER_MAX_BYTES,
    GateRunIdentity,
    PreflightError,
    assert_clean_working_tree,
    assert_keep_on_failure_allowed,
    assert_linux_platform,
    assert_valid_run_id,
    build_gate_environment,
    generate_run_id,
    resolve_git_sha,
)

# --- run_id -----------------------------------------------------------


def test_generate_run_id_is_filesystem_and_db_name_safe():
    run_id = generate_run_id()

    assert_valid_run_id(run_id)  # does not raise
    assert run_id == run_id.lower()
    assert " " not in run_id
    assert "-" not in run_id


@pytest.mark.parametrize("bad_id", ["Has-Dash", "has space", "UPPER", "semi;colon", ""])
def test_assert_valid_run_id_rejects_unsafe_values(bad_id):
    with pytest.raises(PreflightError):
        assert_valid_run_id(bad_id)


def test_generate_run_id_is_unique_across_calls():
    assert generate_run_id() != generate_run_id()


# --- checkpoint corrective 3 (finding #1): PostgreSQL identifier length ----


def test_max_gate_run_id_bytes_is_derived_from_postgres_limit_and_longest_prefix():
    """PostgreSQL's identifier limit is 63 bytes; the longest canonical
    gate database name prefix is `linuxlab_gate_restore_central_`
    (see scripts/release_gate/database.py's canonical name construction)
    -- the run-id byte bound must be derived from these, not a bare
    magic number."""
    assert POSTGRES_IDENTIFIER_MAX_BYTES == 63
    assert LONGEST_GATE_DB_PREFIX == "linuxlab_gate_restore_central_"
    assert MAX_GATE_RUN_ID_BYTES == POSTGRES_IDENTIFIER_MAX_BYTES - len(
        LONGEST_GATE_DB_PREFIX.encode("ascii")
    )
    assert MAX_GATE_RUN_ID_BYTES == 33


def test_assert_valid_run_id_accepts_exactly_the_maximum_byte_length():
    run_id = "r" + "a" * (MAX_GATE_RUN_ID_BYTES - 1)
    assert len(run_id.encode("ascii")) == MAX_GATE_RUN_ID_BYTES == 33

    assert_valid_run_id(run_id)  # does not raise


def test_assert_valid_run_id_rejects_one_byte_over_the_maximum():
    run_id = "r" + "a" * MAX_GATE_RUN_ID_BYTES  # 34 bytes total
    assert len(run_id.encode("ascii")) == MAX_GATE_RUN_ID_BYTES + 1 == 34

    with pytest.raises(PreflightError):
        assert_valid_run_id(run_id)


def test_assert_valid_run_id_rejects_a_113_byte_run_id():
    """The exact reviewer probe."""
    run_id = "r" + "a" * 112
    assert len(run_id) == 113

    with pytest.raises(PreflightError):
        assert_valid_run_id(run_id)


def test_generate_run_id_always_satisfies_the_byte_limit():
    for _ in range(50):
        run_id = generate_run_id()
        assert len(run_id.encode("ascii")) <= MAX_GATE_RUN_ID_BYTES
        assert_valid_run_id(run_id)  # does not raise


# --- git state ----------------------------------------------------------


def test_resolve_git_sha_returns_current_head(tmp_path):
    _init_git_repo(tmp_path)
    sha = resolve_git_sha(tmp_path)

    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_assert_clean_working_tree_passes_on_clean_repo(tmp_path):
    _init_git_repo(tmp_path)

    assert_clean_working_tree(tmp_path)  # does not raise


def test_assert_clean_working_tree_rejects_dirty_repo(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted")

    with pytest.raises(PreflightError):
        assert_clean_working_tree(tmp_path)


def _init_git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "gate@example.org"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Gate"], cwd=path, check=True)
    (path / "README.md").write_text("gate test repo")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


# --- platform -------------------------------------------------------------


def test_assert_linux_platform_accepts_linux():
    assert_linux_platform(platform_name="Linux")  # does not raise


@pytest.mark.parametrize("platform_name", ["Darwin", "Windows", ""])
def test_assert_linux_platform_rejects_non_linux(platform_name):
    with pytest.raises(PreflightError):
        assert_linux_platform(platform_name=platform_name)


# --- --keep-on-failure safety ------------------------------------------


def test_keep_on_failure_allowed_locally():
    assert_keep_on_failure_allowed(True, env={})  # does not raise


def test_keep_on_failure_rejected_under_ci():
    with pytest.raises(PreflightError):
        assert_keep_on_failure_allowed(True, env={"CI": "true"})


def test_keep_on_failure_false_is_always_allowed_even_under_ci():
    assert_keep_on_failure_allowed(False, env={"CI": "true"})  # does not raise


# --- full environment resolution -------------------------------------------


def test_build_gate_environment_produces_run_identity(tmp_path):
    _init_git_repo(tmp_path)

    environment = build_gate_environment(repo_root=tmp_path, env={})

    assert isinstance(environment.run_id, str)
    assert_valid_run_id(environment.run_id)
    assert len(environment.git_sha) == 40
    assert environment.artifact_dir.name == environment.run_id
    assert "phase1-release" in str(environment.artifact_dir)


def test_build_gate_environment_rejects_dirty_tree(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted")

    with pytest.raises(PreflightError):
        build_gate_environment(repo_root=tmp_path, env={})


def test_build_gate_environment_resolves_db_admin_from_env(tmp_path):
    _init_git_repo(tmp_path)

    environment = build_gate_environment(
        repo_root=tmp_path,
        env={
            "POSTGRES_HOST": "db.example.org",
            "POSTGRES_PORT": "5433",
            "POSTGRES_USER": "gate_admin",
            "POSTGRES_PASSWORD": "PRIVATE_KEY_DO_NOT_LEAK",
        },
    )

    assert environment.db_admin.host == "db.example.org"
    assert environment.db_admin.port == "5433"
    assert environment.db_admin.user == "gate_admin"
    assert environment.db_admin.password == "PRIVATE_KEY_DO_NOT_LEAK"
    # The password must never appear in repr()/str() -- only ever read
    # as an explicit attribute access by code that needs it.
    assert "PRIVATE_KEY_DO_NOT_LEAK" not in repr(environment)
    assert "PRIVATE_KEY_DO_NOT_LEAK" not in repr(environment.db_admin)


def test_gate_run_identity_is_a_frozen_value_object():
    identity = GateRunIdentity(run_id="r" + "a" * 12, git_sha="a" * 40)

    with pytest.raises(AttributeError):
        identity.run_id = "different"
