"""Task 15 Tasks 8-10: shared real dual-deployment E2E test infrastructure.

Not a test module itself -- provides `dual_deployment()`, a context
manager that stands up two genuinely separate ephemeral PostgreSQL
databases, two real Gunicorn processes (Central, Node), and a real
issued Node sync credential, then tears everything down (processes,
then databases) in `finally` regardless of outcome. Task 8's happy path
and every Task 9 negative scenario share this one setup/teardown so
~150 lines of dual-deployment bootstrapping is not duplicated six times.

Every action that needs Django ORM access against a specific ephemeral
database goes through a real subprocess (scripts/release_gate/cli.py or
`manage.py <command>`), exactly like scripts.release_gate.database.
run_migrations() already does for `manage.py migrate` -- never an
in-process multi-database Django configuration.
"""

import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from scripts.release_gate.database import (
    GateDatabaseNames,
    create_database,
    drop_database,
    run_migrations,
)
from scripts.release_gate.environment import DatabaseAdminConnection, generate_run_id
from scripts.release_gate.processes import (
    reserve_listener,
    start_gunicorn,
    stop_process,
    wait_ready,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "release_gate" / "cli.py"
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

READY_TIMEOUT_SECONDS = 20


class ActionError(Exception):
    """Raised when a scripts/release_gate/cli.py subprocess action fails.
    Carries the parsed {"error": ...} payload for caller-side diagnosis."""


def db_admin_from_env():
    return DatabaseAdminConnection(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ.get("POSTGRES_USER", "linuxlab"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )


def run_action(profile_env, *args, allow_failure=False):
    """Invoke scripts/release_gate/cli.py as a real subprocess under
    `profile_env` and return its parsed JSON result. Raises ActionError
    on a non-zero exit unless `allow_failure=True` (some Task 9 negative
    scenarios expect and assert on a failure result directly)."""
    result = subprocess.run(
        [str(PYTHON), str(CLI_PATH), *args],
        cwd=str(REPO_ROOT),
        env={**os.environ, **profile_env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ActionError(
            f"cli.py {args} produced non-JSON output "
            f"(exit {result.returncode}): stdout={result.stdout!r} stderr={result.stderr!r}"
        ) from exc

    if result.returncode != 0 and not allow_failure:
        raise ActionError(f"cli.py {args} failed: {payload}")
    return payload


def run_sync_worker_once(node_profile_env):
    """Trigger exactly one Node outbox flush cycle through the real
    production sync worker entry point (`manage.py sync_events --once`)
    -- never a bespoke bridge re-implementation of flush_outbox()."""
    result = subprocess.run(
        [str(PYTHON), "manage.py", "sync_events", "--once"],
        cwd=str(REPO_ROOT),
        env={**os.environ, **node_profile_env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result


@dataclass
class DualDeployment:
    run_id: str
    gate_names: GateDatabaseNames
    db_admin: DatabaseAdminConnection
    tmp_dir: Path
    central: object  # ManagedProcess
    node: object  # ManagedProcess
    central_profile_env: dict
    node_profile_env: dict
    node_id: str
    node_secret: str
    private_key_path: Path
    public_key_path: Path


def _write_keypair(tmp_dir):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_key_path = tmp_dir / "release-gate-signing.pem"
    public_key_path = tmp_dir / "release-gate-trusted.pem"
    private_key_path.write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    public_key_path.write_bytes(public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    return private_key_path, public_key_path


@contextmanager
def dual_deployment(tmp_path, *, sync_enabled=True, start_node=True):
    """Stand up two real ephemeral PostgreSQL databases and (unless
    `start_node=False`, for scenarios that need Node offline/absent) two
    real Gunicorn processes, with a real issued Node sync credential
    already configured into the Node process's environment. Tears
    everything down in `finally`: processes first, then databases,
    regardless of what the caller's `with` block does.
    """
    run_id = generate_run_id()
    gate_names = GateDatabaseNames.for_run(run_id)
    db_admin = db_admin_from_env()

    create_database(db_admin, gate_names, "central")
    create_database(db_admin, gate_names, "node")

    central = None
    node = None
    try:
        central_profile_env = {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}
        node_profile_env_for_migrate = {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.node"}
        run_migrations(gate_names, "central", central_profile_env, repo_root=REPO_ROOT)
        run_migrations(gate_names, "node", node_profile_env_for_migrate, repo_root=REPO_ROOT)

        private_key_path, public_key_path = _write_keypair(tmp_path)

        central_listener = reserve_listener()
        central_profile_env = {
            "DJANGO_SETTINGS_MODULE": "linuxlab.settings.central",
            "POSTGRES_DB": gate_names.central,
        }
        central = start_gunicorn(central_profile_env, central_listener, tmp_path / "central.log")
        wait_ready(central, timeout_seconds=READY_TIMEOUT_SECONDS)

        credential = run_action(
            central_profile_env, "issue-node-credential", "--display-name", f"gate-e2e-{run_id}"
        )

        node_profile_env = {
            "DJANGO_SETTINGS_MODULE": "linuxlab.settings.node",
            "POSTGRES_DB": gate_names.node,
            "LINUXLAB_SYNC_ENABLED": "true" if sync_enabled else "false",
            "LINUXLAB_NODE_ID": credential["node_id"],
            "LINUXLAB_SYNC_SECRET": credential["secret"],
            "LINUXLAB_CENTRAL_URL": central.base_url,
            "LINUXLAB_SYNC_ALLOW_INSECURE_HTTP": "true",
        }

        if start_node:
            node_listener = reserve_listener()
            node = start_gunicorn(node_profile_env, node_listener, tmp_path / "node.log")
            wait_ready(node, timeout_seconds=READY_TIMEOUT_SECONDS)

        yield DualDeployment(
            run_id=run_id,
            gate_names=gate_names,
            db_admin=db_admin,
            tmp_dir=tmp_path,
            central=central,
            node=node,
            central_profile_env=central_profile_env,
            node_profile_env=node_profile_env,
            node_id=credential["node_id"],
            node_secret=credential["secret"],
            private_key_path=private_key_path,
            public_key_path=public_key_path,
        )
    finally:
        if node is not None:
            stop_process(node)
        if central is not None:
            stop_process(central)
        drop_database(db_admin, gate_names, "node")
        drop_database(db_admin, gate_names, "central")


def author_and_package(deployment, *, prompt, options, correct_option_id, max_score=1):
    """Central: author + publish + build + sign one real single-choice
    assessment activity, returning identities plus the local package
    file path. Combines three cli.py calls that Task 8/9 scenarios
    otherwise always need together."""
    authored = run_action(
        deployment.central_profile_env,
        "author-and-publish",
        "--prompt",
        prompt,
        "--options-json",
        json.dumps(options),
        "--correct-option-id",
        correct_option_id,
        "--max-score",
        str(max_score),
    )

    package_path = deployment.tmp_dir / f"package-{authored['source_public_id']}-v{authored['package_version']}.llpkg"
    run_action(
        deployment.central_profile_env,
        "build-and-sign-package",
        "--source-public-id",
        authored["source_public_id"],
        "--package-version",
        str(authored["package_version"]),
        "--private-key-file",
        str(deployment.private_key_path),
        "--output-file",
        str(package_path),
    )

    return authored, package_path


def import_and_activate(deployment, package_path):
    return run_action(
        deployment.node_profile_env,
        "import-and-activate-package",
        "--package-file",
        str(package_path),
        "--public-key-file",
        str(deployment.public_key_path),
    )
