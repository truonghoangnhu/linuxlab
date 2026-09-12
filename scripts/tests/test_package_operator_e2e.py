import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

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
    environment can be asserted on directly (Section 16: the Node
    subprocess environment must never carry the Central database name),
    without needing to actually spawn a subprocess to inspect it."""
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


def _run_node_subprocess_command(*, node_db, request):
    """Every Node-flavored call in this test goes through here rather
    than `_run_subprocess_command()` directly, so the Central private
    signing key can structurally never reach a Node invocation: this
    assertion fails loudly (not silently) the moment any Node request
    literal above is ever edited to accidentally include one."""
    assert "signing_key_path" not in request, (
        "Node subprocess request must never carry a Central signing-key path"
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


@pytest.fixture
def package_operator_e2e_databases():
    db_admin = _db_admin_from_env()
    run_id = f"pkgope2e{secrets.token_hex(4)}"
    gate_names = GateDatabaseNames.for_run(run_id)

    create_database(db_admin, gate_names, "central")
    run_migrations(
        gate_names, "central", {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}
    )

    create_database(db_admin, gate_names, "node")
    run_migrations(gate_names, "node", {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.node"})

    try:
        yield {"db_admin": db_admin, "gate_names": gate_names}
    finally:
        # Cleanup runs on pass AND failure (this is inside `finally`),
        # and both databases are attempted even if one drop fails, so a
        # Node cleanup failure never skips Central cleanup (matching
        # database.py's own documented cleanup invariant).
        errors = []
        for member in ("central", "node"):
            try:
                drop_database(db_admin, gate_names, member)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            raise RuntimeError(f"ephemeral database cleanup failed: {errors}")


def test_node_subprocess_environment_never_carries_central_database_name():
    """Section 16: the Node-flavored subprocess environment must pin
    POSTGRES_DB to the Node database and must not carry the Central
    database name anywhere in its values. Pure environment-construction
    check -- no real database or subprocess needed."""
    central_db = "linuxlab_gate_central_pkgope2edeadbeef"
    node_db = "linuxlab_gate_node_pkgope2edeadbeef"

    node_env = _build_subprocess_env(settings_module="linuxlab.settings.node", db_name=node_db)

    assert node_env["POSTGRES_DB"] == node_db
    assert node_env["DJANGO_SETTINGS_MODULE"] == "linuxlab.settings.node"
    assert central_db not in node_env.values()


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

    # --- fixture/bootstrap: test-only trust material, written only
    # under tmp_path (cleaned up by pytest itself) -- never the real
    # /etc/linuxlab path, and the private key file is never referenced
    # by any Node-flavored request below. ---
    private_key = Ed25519PrivateKey.generate()
    signing_key_path = tmp_path / "package-signing.key"
    signing_key_path.write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    trust_anchor_path = tmp_path / "package-signing-public.key"
    trust_anchor_path.write_bytes(
        private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )

    # --- database isolation proof, BEFORE any Central or Node command
    # runs: the freshly migrated Node database has zero rows in either
    # a Central-owned table or its own lifecycle table. ---
    assert _count_rows(db_admin, node_db, "publication_publicationsnapshot") == 0
    assert _count_rows(db_admin, node_db, "sync_installedpackage") == 0

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

    # --- Central: publish + build version A, through the CLI only ---
    publish_a = _run_central_subprocess_command(
        central_db=central_db,
        request={
            "mode": "command",
            "signing_key_path": str(signing_key_path),
            "command": [
                "manage_central_package", "publish", "--learning-path", learning_path_id
            ],
        },
    )
    assert publish_a.returncode == 0, publish_a.stderr
    assert "Version: 1" in publish_a.stdout

    package_a_path = tmp_path / "package-a.llpkg"
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

    # --- pure file boundary: from here on, only package_a_path's bytes
    # reach Node. Every Node-flavored request below is dispatched through
    # _run_node_subprocess_command(), which asserts no signing_key_path
    # is ever present, and never mentions central_db at all -- it is not
    # merely unused, it is never constructed. ---

    # --- Node: verify (read-only) ---
    before = _count_rows(db_admin, node_db, "sync_installedpackage")
    verify_a = _run_node_subprocess_command(
        node_db=node_db,
        request={
            "mode": "command",
            "trust_anchor_path": str(trust_anchor_path),
            "command": ["manage_node_package", "verify", "--file", str(package_a_path)],
        },
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
    )
    assert import_a.returncode == 0, import_a.stderr

    status_after_import = _run_node_subprocess_command(
        node_db=node_db,
        request={"mode": "command", "command": ["manage_node_package", "status"]},
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
    )
    assert activate_a.returncode == 0, activate_a.stderr
    assert "State: active" in activate_a.stdout

    # --- Central: version B, through the same operator workflow ---
    publish_b = _run_central_subprocess_command(
        central_db=central_db,
        request={
            "mode": "command",
            "signing_key_path": str(signing_key_path),
            "command": [
                "manage_central_package", "publish", "--learning-path", learning_path_id
            ],
        },
    )
    assert publish_b.returncode == 0, publish_b.stderr
    assert "Version: 2" in publish_b.stdout

    package_b_path = tmp_path / "package-b.llpkg"
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

    # --- Node: version B ---
    verify_b = _run_node_subprocess_command(
        node_db=node_db,
        request={
            "mode": "command",
            "trust_anchor_path": str(trust_anchor_path),
            "command": ["manage_node_package", "verify", "--file", str(package_b_path)],
        },
    )
    assert verify_b.returncode == 0, verify_b.stderr

    import_b = _run_node_subprocess_command(
        node_db=node_db,
        request={
            "mode": "command",
            "trust_anchor_path": str(trust_anchor_path),
            "command": ["manage_node_package", "import", "--file", str(package_b_path)],
        },
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
    )
    assert rollback.returncode == 0, rollback.stderr
    assert "Version: 1" in rollback.stdout
    assert "State: active" in rollback.stdout

    status_final = _run_node_subprocess_command(
        node_db=node_db,
        request={"mode": "command", "command": ["manage_node_package", "status"]},
    )
    assert status_final.stdout.count("State: active") == 1

    # --- Node never used the Central database: application state
    # mutation from the entire scenario landed only in NODE_DB, never
    # CENTRAL_DB (Section 19). ---
    assert _count_rows(db_admin, central_db, "sync_installedpackage") == 0
