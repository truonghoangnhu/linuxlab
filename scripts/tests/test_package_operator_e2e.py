import json
import os
import secrets
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql

from scripts.release_gate.database import (
    GateDatabaseNames,
    create_database,
    drop_database,
    run_migrations,
)
from scripts.release_gate.environment import DatabaseAdminConnection

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SUBPROCESS_SCRIPT = Path(__file__).parent / "_package_operator_subprocess.py"


def _db_admin_from_env():
    return DatabaseAdminConnection(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ.get("POSTGRES_USER", "linuxlab"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )


def _build_subprocess_env(*, settings_module, db_name):
    """Isolated from `_run_subprocess_command()` so the constructed
    environment can be asserted on directly (M1: proving it OVERRIDES a
    poisoned inherited value, not merely that a fabricated name is
    absent from an already-clean environment), without needing to
    actually spawn a subprocess to inspect it."""
    return {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": settings_module,
        "POSTGRES_DB": db_name,
    }


def _run_subprocess_command(*, settings_module, db_name, request):
    child_env = _build_subprocess_env(settings_module=settings_module, db_name=db_name)
    return subprocess.run(
        [sys.executable, str(_SUBPROCESS_SCRIPT), json.dumps(request)],
        cwd=str(_REPO_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_node_subprocess_command(*, node_db, request, private_key_path):
    """Every Node-flavored call in this test goes through here rather
    than `_run_subprocess_command()` directly, so the Central private
    signing key can structurally never reach a Node invocation, in two
    independent ways:

    1. `signing_key_path` can never be present in the JSON request (the
       original Task-7 guard).
    2. (Independent review Important I2) the private key FILE itself
       must not exist on disk at the moment ANY Node subprocess is
       spawned. Node runs as the same OS user as the test harness, so a
       sibling file that merely isn't referenced in Node's own
       request/environment would still be readable by Node -- request
       isolation is not filesystem isolation. `private_key_path` is
       supplied by the caller SOLELY for this local pre-spawn assertion;
       it is never put into `request`, `child_env`, argv, or stdin.
    """
    assert "signing_key_path" not in request, (
        "Node subprocess request must never carry a Central signing-key path"
    )
    assert not private_key_path.exists(), (
        f"private signing key file must not exist while a Node subprocess runs: "
        f"{private_key_path}"
    )
    return _run_subprocess_command(
        settings_module="linuxlab.settings.node", db_name=node_db, request=request
    )


def _run_central_subprocess_command(*, central_db, request):
    return _run_subprocess_command(
        settings_module="linuxlab.settings.central", db_name=central_db, request=request
    )


def _count_rows(db_admin, db_name, table_name):
    conn = psycopg.connect(
        host=db_admin.host, port=db_admin.port, user=db_admin.user,
        password=db_admin.password, dbname=db_name,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table_name)))
            return cursor.fetchone()[0]
    finally:
        conn.close()


@contextmanager
def _ephemeral_database_pair(db_admin, gate_names):
    """Independent review Important I1: cleanup ownership of an
    ephemeral database begins the instant its own `create_database()`
    call succeeds -- not after both Central and Node are fully set up.
    A Central migration failure, a Node creation failure, or a Node
    migration failure must each still drop every database that was
    ACTUALLY created so far, and never attempt to drop one that was not.

    A cleanup (drop) failure must never replace/mask an already-
    propagating primary setup/test failure: when one is propagating,
    every cleanup error is attached to it via `add_note()` (PEP 678)
    rather than raised in its place. When there is no primary failure
    (setup and the test body both succeeded) and cleanup itself is the
    only failure, it is raised on its own so it is never silently lost.
    """
    created = []
    try:
        create_database(db_admin, gate_names, "central")
        created.append("central")
        run_migrations(
            gate_names, "central", {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}
        )

        create_database(db_admin, gate_names, "node")
        created.append("node")
        run_migrations(gate_names, "node", {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.node"})

        yield
    finally:
        primary_exc = sys.exc_info()[1]
        cleanup_errors = []
        for member in created:
            try:
                drop_database(db_admin, gate_names, member)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
        if cleanup_errors:
            if primary_exc is not None:
                for exc in cleanup_errors:
                    primary_exc.add_note(f"ephemeral database cleanup also failed: {exc!r}")
            else:
                raise RuntimeError(f"ephemeral database cleanup failed: {cleanup_errors}")


@pytest.fixture
def package_operator_e2e_databases():
    db_admin = _db_admin_from_env()
    run_id = f"pkgope2e{secrets.token_hex(4)}"
    gate_names = GateDatabaseNames.for_run(run_id)

    with _ephemeral_database_pair(db_admin, gate_names):
        yield {"db_admin": db_admin, "gate_names": gate_names}


@contextmanager
def _materialized_private_key(private_key_path, pem_bytes):
    """Independent review Important I2 root-cause fix: the Central
    private signing key must exist on disk only for the narrow window a
    single Central `build` subprocess needs it -- never while any Node
    subprocess runs, and never left behind after a failed Central build.
    Writes `pem_bytes` to `private_key_path`, yields for exactly one
    Central build call, and unlinks the file in `finally` regardless of
    whether that call succeeds. The caller (the parent pytest process)
    is the only place the private PEM bytes/key object ever exist
    in-memory across the whole scenario.
    """
    assert not private_key_path.exists(), (
        f"private signing key file must not already exist at {private_key_path}"
    )
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(private_key_path.parent, 0o700)
    private_key_path.write_bytes(pem_bytes)
    os.chmod(private_key_path, 0o600)
    try:
        yield
    finally:
        private_key_path.unlink(missing_ok=True)


# --- Important I1: database cleanup ownership -------------------------------


class _SentinelSetupError(Exception):
    pass


def _fake_gate_names():
    return SimpleNamespace(central="fake-central-db", node="fake-node-db")


def test_ephemeral_database_pair_cleans_up_central_on_central_migration_failure(monkeypatch):
    """I1 RED->GREEN: Central create succeeds, Central migration raises.
    Central's drop must be attempted; Node must never be created or
    dropped (execution never reached it); the sentinel failure remains
    the failure that propagates."""
    e2e_mod = sys.modules[__name__]

    calls = []

    def fake_create_database(db_admin, gate_names, member):
        calls.append(("create", member))

    def fake_run_migrations(gate_names, member, profile_env):
        calls.append(("migrate", member))
        if member == "central":
            raise _SentinelSetupError("central migration boom")

    def fake_drop_database(db_admin, gate_names, member):
        calls.append(("drop", member))

    monkeypatch.setattr(e2e_mod, "create_database", fake_create_database)
    monkeypatch.setattr(e2e_mod, "run_migrations", fake_run_migrations)
    monkeypatch.setattr(e2e_mod, "drop_database", fake_drop_database)

    with pytest.raises(_SentinelSetupError), e2e_mod._ephemeral_database_pair(db_admin=None, gate_names=_fake_gate_names()):
            pytest.fail("must never reach the yielded body")

    assert calls == [("create", "central"), ("migrate", "central"), ("drop", "central")]


def test_ephemeral_database_pair_does_not_drop_node_when_node_creation_fails(monkeypatch):
    """I1: Central created+migrated; Node creation raises. Central drop
    must be attempted; Node drop must NOT be attempted (it was never
    successfully created)."""
    e2e_mod = sys.modules[__name__]

    calls = []

    def fake_create_database(db_admin, gate_names, member):
        calls.append(("create", member))
        if member == "node":
            raise _SentinelSetupError("node create boom")

    def fake_run_migrations(gate_names, member, profile_env):
        calls.append(("migrate", member))

    def fake_drop_database(db_admin, gate_names, member):
        calls.append(("drop", member))

    monkeypatch.setattr(e2e_mod, "create_database", fake_create_database)
    monkeypatch.setattr(e2e_mod, "run_migrations", fake_run_migrations)
    monkeypatch.setattr(e2e_mod, "drop_database", fake_drop_database)

    with pytest.raises(_SentinelSetupError), e2e_mod._ephemeral_database_pair(db_admin=None, gate_names=_fake_gate_names()):
            pytest.fail("must never reach the yielded body")

    assert ("create", "central") in calls
    assert ("migrate", "central") in calls
    assert ("create", "node") in calls
    assert ("drop", "central") in calls
    assert ("migrate", "node") not in calls
    assert ("drop", "node") not in calls


def test_ephemeral_database_pair_drops_both_when_node_migration_fails(monkeypatch):
    """I1: Central created+migrated, Node created, Node migration raises.
    Both successfully created databases must be dropped."""
    e2e_mod = sys.modules[__name__]

    calls = []

    def fake_create_database(db_admin, gate_names, member):
        calls.append(("create", member))

    def fake_run_migrations(gate_names, member, profile_env):
        calls.append(("migrate", member))
        if member == "node":
            raise _SentinelSetupError("node migration boom")

    def fake_drop_database(db_admin, gate_names, member):
        calls.append(("drop", member))

    monkeypatch.setattr(e2e_mod, "create_database", fake_create_database)
    monkeypatch.setattr(e2e_mod, "run_migrations", fake_run_migrations)
    monkeypatch.setattr(e2e_mod, "drop_database", fake_drop_database)

    with pytest.raises(_SentinelSetupError), e2e_mod._ephemeral_database_pair(db_admin=None, gate_names=_fake_gate_names()):
            pytest.fail("must never reach the yielded body")

    assert ("create", "central") in calls
    assert ("create", "node") in calls
    assert ("drop", "central") in calls
    assert ("drop", "node") in calls


def test_ephemeral_database_pair_second_drop_attempted_when_first_drop_fails(monkeypatch):
    """I1 cleanup-failure precedence, half 1: both databases were
    created, the primary failure is the Node migration, and the Central
    drop itself ALSO fails. The Node drop must still be attempted (a
    Central cleanup failure must never skip Node cleanup), and the
    ORIGINAL migration failure -- not the drop failure -- must be what
    ultimately propagates, with the drop failure attached as a note."""
    e2e_mod = sys.modules[__name__]

    calls = []

    def fake_create_database(db_admin, gate_names, member):
        calls.append(("create", member))

    def fake_run_migrations(gate_names, member, profile_env):
        calls.append(("migrate", member))
        if member == "node":
            raise _SentinelSetupError("node migration boom")

    def fake_drop_database(db_admin, gate_names, member):
        calls.append(("drop", member))
        if member == "central":
            raise RuntimeError("central drop boom")

    monkeypatch.setattr(e2e_mod, "create_database", fake_create_database)
    monkeypatch.setattr(e2e_mod, "run_migrations", fake_run_migrations)
    monkeypatch.setattr(e2e_mod, "drop_database", fake_drop_database)

    with (
        pytest.raises(_SentinelSetupError) as excinfo,
        e2e_mod._ephemeral_database_pair(db_admin=None, gate_names=_fake_gate_names()),
    ):
        pytest.fail("must never reach the yielded body")

    assert ("drop", "central") in calls
    assert ("drop", "node") in calls
    assert str(excinfo.value) == "node migration boom"
    assert any("central drop boom" in note for note in getattr(excinfo.value, "__notes__", []))


def test_ephemeral_database_pair_raises_cleanup_error_when_only_cleanup_fails(monkeypatch):
    """I1 cleanup-failure precedence, half 2: setup and the test body
    both succeed, but a drop itself fails. With no primary failure to
    protect, the cleanup failure must surface on its own rather than be
    silently swallowed."""
    e2e_mod = sys.modules[__name__]

    def fake_create_database(db_admin, gate_names, member):
        pass

    def fake_run_migrations(gate_names, member, profile_env):
        pass

    def fake_drop_database(db_admin, gate_names, member):
        raise RuntimeError(f"{member} drop boom")

    monkeypatch.setattr(e2e_mod, "create_database", fake_create_database)
    monkeypatch.setattr(e2e_mod, "run_migrations", fake_run_migrations)
    monkeypatch.setattr(e2e_mod, "drop_database", fake_drop_database)

    with pytest.raises(RuntimeError, match="ephemeral database cleanup failed"), e2e_mod._ephemeral_database_pair(db_admin=None, gate_names=_fake_gate_names()):
            pass


# --- Important I2: private-key filesystem lifetime --------------------------


def test_run_node_subprocess_command_refuses_to_run_while_private_key_file_exists(tmp_path):
    """I2 RED->GREEN: the guarded Node wrapper must refuse to spawn a
    Node subprocess while the private signing key file still exists on
    disk, even though the request/environment never reference it."""
    e2e_mod = sys.modules[__name__]

    private_key_path = tmp_path / "central-keys" / "package-signing.key"
    private_key_path.parent.mkdir(parents=True)
    private_key_path.write_bytes(b"SENTINEL_PRIVATE_KEY_BYTES")

    with pytest.raises(AssertionError):
        e2e_mod._run_node_subprocess_command(
            node_db="irrelevant-never-connected-to",
            request={"mode": "command", "command": ["manage_node_package", "status"]},
            private_key_path=private_key_path,
        )


def test_materialized_private_key_refuses_to_overwrite_existing_file(tmp_path):
    private_key_path = tmp_path / "central-keys" / "package-signing.key"
    private_key_path.parent.mkdir(parents=True)
    private_key_path.write_bytes(b"PRE_EXISTING")

    with pytest.raises(AssertionError), _materialized_private_key(private_key_path, b"NEW_PEM_BYTES"):
            pytest.fail("must never reach the yielded body")


def test_materialized_private_key_unlinks_file_even_when_central_build_fails():
    """I2: private test key material must not remain on disk after a
    failed Central build -- independent of pytest's own tmp_path
    retention policy, so a plain (non-fixture) temporary directory is
    used and cleaned up by this test itself."""
    import tempfile

    class _FakeBuildError(Exception):
        pass

    with tempfile.TemporaryDirectory() as tmp:
        private_key_path = Path(tmp) / "central-keys" / "package-signing.key"

        with pytest.raises(_FakeBuildError), _materialized_private_key(private_key_path, b"FAKE_PEM_BYTES_FOR_TEST"):
                assert private_key_path.exists()
                raise _FakeBuildError("central build boom")

        assert not private_key_path.exists()


# --- Minor M1: adversarial environment isolation ----------------------------


def test_node_subprocess_environment_overrides_poisoned_inherited_values(monkeypatch):
    """M1: prove the Node subprocess environment actually OVERRIDES a
    conflicting inherited value for the exact keys LinuxLab's settings
    consume (POSTGRES_DB, DJANGO_SETTINGS_MODULE) -- not merely that a
    fabricated Central db name string happens to be absent from an
    already-clean environment, which would pass vacuously without
    proving any override behavior. Does not claim an arbitrary
    environment allow-list: the helper intentionally copies the rest of
    os.environ unchanged."""
    monkeypatch.setenv("POSTGRES_DB", "linuxlab_gate_central_poisoned")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "linuxlab.settings.central")

    node_db = "linuxlab_gate_node_pkgope2edeadbeef"
    node_env = _build_subprocess_env(settings_module="linuxlab.settings.node", db_name=node_db)

    assert node_env["POSTGRES_DB"] == node_db
    assert node_env["DJANGO_SETTINGS_MODULE"] == "linuxlab.settings.node"


def test_central_subprocess_environment_overrides_poisoned_inherited_values(monkeypatch):
    """M1 symmetry (Section 21): the same override proof for Central."""
    monkeypatch.setenv("POSTGRES_DB", "linuxlab_gate_node_poisoned")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "linuxlab.settings.node")

    central_db = "linuxlab_gate_central_pkgope2edeadbeef"
    central_env = _build_subprocess_env(
        settings_module="linuxlab.settings.central", db_name=central_db
    )

    assert central_env["POSTGRES_DB"] == central_db
    assert central_env["DJANGO_SETTINGS_MODULE"] == "linuxlab.settings.central"


# --- real database fixture and full scenario --------------------------------


def test_databases_are_created_independently_migrated_and_empty(package_operator_e2e_databases):
    """Checkpoint 1 (Task-7 STOP-condition re-verification): prove the
    reused release-gate database helpers actually produce two separately
    migrated, empty databases outside a full release-gate run, before
    building the rest of the CLI-driven scenario on top of them."""
    db_admin = package_operator_e2e_databases["db_admin"]
    gate_names = package_operator_e2e_databases["gate_names"]

    assert _count_rows(db_admin, gate_names.central, "publication_publicationsnapshot") == 0
    assert _count_rows(db_admin, gate_names.node, "sync_installedpackage") == 0


def test_full_operator_workflow_central_to_node(package_operator_e2e_databases, tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    db_admin = package_operator_e2e_databases["db_admin"]
    gate_names = package_operator_e2e_databases["gate_names"]
    central_db = gate_names.central
    node_db = gate_names.node

    # --- fixture/bootstrap: one real ephemeral Ed25519 keypair. The
    # private PEM bytes live ONLY in this parent process's memory except
    # for the narrow _materialized_private_key() window around each
    # Central build call below (Important I2). The public key lives in a
    # SEPARATE directory from wherever the private key is ever
    # materialized, and stays on disk for the whole scenario since it is
    # not secret. ---
    private_key = Ed25519PrivateKey.generate()
    private_pem_bytes = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    signing_key_path = tmp_path / "central-keys" / "package-signing.key"

    node_keys_dir = tmp_path / "node-keys"
    node_keys_dir.mkdir()
    trust_anchor_path = node_keys_dir / "package-signing-public.key"
    trust_anchor_path.write_bytes(
        private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )

    # --- database isolation proof, BEFORE any Central or Node command
    # runs: the freshly migrated Node database has zero rows in either
    # a Central-owned table or its own lifecycle table, and the private
    # key has never been written yet. ---
    assert _count_rows(db_admin, node_db, "publication_publicationsnapshot") == 0
    assert _count_rows(db_admin, node_db, "sync_installedpackage") == 0
    assert not signing_key_path.exists()

    # --- Central: bootstrap one LearningPathPage directly in the
    # Central database (authoring fixture only, not a lifecycle step
    # this test proves via CLI). ---
    bootstrap = _run_central_subprocess_command(
        central_db=central_db,
        request={
            "mode": "bootstrap_learning_path",
            "title": "E2E Path",
            "slug": f"e2e-{uuid.uuid4().hex[:8]}",
        },
    )
    assert bootstrap.returncode == 0, bootstrap.stderr
    learning_path_id = bootstrap.stdout.strip().removeprefix("LEARNING_PATH_PUBLIC_ID:")

    # --- Central: publish version A. publish never signs anything, so
    # it needs no signing key at all -- the private key file is created
    # only around the build call below. ---
    publish_a = _run_central_subprocess_command(
        central_db=central_db,
        request={
            "mode": "command",
            "command": [
                "manage_central_package", "publish", "--learning-path", learning_path_id
            ],
        },
    )
    assert publish_a.returncode == 0, publish_a.stderr
    assert "Version: 1" in publish_a.stdout

    # --- Central: build version A -- the private key exists on disk
    # ONLY for the duration of this one subprocess call. ---
    package_a_path = tmp_path / "package-a.llpkg"
    with _materialized_private_key(signing_key_path, private_pem_bytes):
        build_a = _run_central_subprocess_command(
            central_db=central_db,
            request={
                "mode": "command",
                "signing_key_path": str(signing_key_path),
                "command": [
                    "manage_central_package", "build",
                    "--learning-path", learning_path_id, "--version", "1",
                    "--output", str(package_a_path),
                ],
            },
        )
    assert build_a.returncode == 0, build_a.stderr
    assert package_a_path.exists()
    assert not signing_key_path.exists()

    # --- pure file boundary: from here on, only package_a_path's bytes
    # reach Node. Every Node-flavored request below is dispatched through
    # _run_node_subprocess_command(), which asserts no signing_key_path
    # is ever present in the request AND that signing_key_path does not
    # exist on disk at the moment of each call. ---

    # --- Node: verify (read-only) ---
    before = _count_rows(db_admin, node_db, "sync_installedpackage")
    verify_a = _run_node_subprocess_command(
        node_db=node_db,
        request={
            "mode": "command",
            "trust_anchor_path": str(trust_anchor_path),
            "command": ["manage_node_package", "verify", "--file", str(package_a_path)],
        },
        private_key_path=signing_key_path,
    )
    assert verify_a.returncode == 0, verify_a.stderr
    assert _count_rows(db_admin, node_db, "sync_installedpackage") == before

    # --- Node: import, status, activate ---
    import_a = _run_node_subprocess_command(
        node_db=node_db,
        request={
            "mode": "command",
            "trust_anchor_path": str(trust_anchor_path),
            "command": ["manage_node_package", "import", "--file", str(package_a_path)],
        },
        private_key_path=signing_key_path,
    )
    assert import_a.returncode == 0, import_a.stderr

    status_after_import = _run_node_subprocess_command(
        node_db=node_db,
        request={"mode": "command", "command": ["manage_node_package", "status"]},
        private_key_path=signing_key_path,
    )
    assert status_after_import.returncode == 0, status_after_import.stderr
    assert "State: staged" in status_after_import.stdout

    activate_a = _run_node_subprocess_command(
        node_db=node_db,
        request={
            "mode": "command",
            "command": [
                "manage_node_package", "activate",
                "--package-type", "learning_path",
                "--source-public-id", learning_path_id,
                "--package-version", "1",
            ],
        },
        private_key_path=signing_key_path,
    )
    assert activate_a.returncode == 0, activate_a.stderr
    assert "State: active" in activate_a.stdout

    # --- Central: version B, through the same operator workflow, reusing
    # the SAME Ed25519 keypair (private_pem_bytes) held in memory since
    # test start -- never a newly generated key. ---
    publish_b = _run_central_subprocess_command(
        central_db=central_db,
        request={
            "mode": "command",
            "command": [
                "manage_central_package", "publish", "--learning-path", learning_path_id
            ],
        },
    )
    assert publish_b.returncode == 0, publish_b.stderr
    assert "Version: 2" in publish_b.stdout

    package_b_path = tmp_path / "package-b.llpkg"
    assert not signing_key_path.exists()
    with _materialized_private_key(signing_key_path, private_pem_bytes):
        build_b = _run_central_subprocess_command(
            central_db=central_db,
            request={
                "mode": "command",
                "signing_key_path": str(signing_key_path),
                "command": [
                    "manage_central_package", "build",
                    "--learning-path", learning_path_id, "--version", "2",
                    "--output", str(package_b_path),
                ],
            },
        )
    assert build_b.returncode == 0, build_b.stderr
    assert not signing_key_path.exists()

    # --- Node: version B ---
    verify_b = _run_node_subprocess_command(
        node_db=node_db,
        request={
            "mode": "command",
            "trust_anchor_path": str(trust_anchor_path),
            "command": ["manage_node_package", "verify", "--file", str(package_b_path)],
        },
        private_key_path=signing_key_path,
    )
    assert verify_b.returncode == 0, verify_b.stderr

    import_b = _run_node_subprocess_command(
        node_db=node_db,
        request={
            "mode": "command",
            "trust_anchor_path": str(trust_anchor_path),
            "command": ["manage_node_package", "import", "--file", str(package_b_path)],
        },
        private_key_path=signing_key_path,
    )
    assert import_b.returncode == 0, import_b.stderr

    activate_b = _run_node_subprocess_command(
        node_db=node_db,
        request={
            "mode": "command",
            "command": [
                "manage_node_package", "activate",
                "--package-type", "learning_path",
                "--source-public-id", learning_path_id,
                "--package-version", "2",
            ],
        },
        private_key_path=signing_key_path,
    )
    assert activate_b.returncode == 0, activate_b.stderr
    assert "State: active" in activate_b.stdout

    # --- Node: rollback to the existing lifecycle's own previous_package ---
    rollback = _run_node_subprocess_command(
        node_db=node_db,
        request={
            "mode": "command",
            "command": [
                "manage_node_package", "rollback",
                "--package-type", "learning_path",
                "--source-public-id", learning_path_id,
            ],
        },
        private_key_path=signing_key_path,
    )
    assert rollback.returncode == 0, rollback.stderr
    assert "Version: 1" in rollback.stdout
    assert "State: active" in rollback.stdout

    status_final = _run_node_subprocess_command(
        node_db=node_db,
        request={"mode": "command", "command": ["manage_node_package", "status"]},
        private_key_path=signing_key_path,
    )
    assert status_final.stdout.count("State: active") == 1

    # --- Node never used the Central database: application state
    # mutation from the entire scenario landed only in NODE_DB, never
    # CENTRAL_DB (Section 19). ---
    assert _count_rows(db_admin, central_db, "sync_installedpackage") == 0

    # --- final confirmation: the private key file was never left behind. ---
    assert not signing_key_path.exists()
