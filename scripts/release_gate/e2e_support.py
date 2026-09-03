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
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from scripts.release_gate.cleanup import remove_workspace, run_cleanup_actions
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
from scripts.release_gate.signing_material import (
    create_ephemeral_signing_keypair,
    delete_private_key,
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


def run_action(profile_env, *args, allow_failure=False, stdin_json=None):
    """Invoke scripts/release_gate/cli.py as a real subprocess under
    `profile_env` and return its parsed JSON result. Raises ActionError
    on a non-zero exit unless `allow_failure=True` (some Task 9 negative
    scenarios expect and assert on a failure result directly).

    `stdin_json`, when given, is JSON-encoded and piped to the child's
    stdin -- the ONLY channel scripts/release_gate/cli.py accepts a
    SENSITIVE action input through (a Node bearer secret, or
    correct-answer/grading material). It is never placed on argv, and
    this function's own error formatting below never echoes the full
    `args` tuple either (only the action name, exit code, and the
    child's own stdout/stderr) -- so a sensitive value could never leak
    into an ActionError's text even by accident (Task 15 Tasks 7-10
    corrective, Critical 2)."""
    action_name = args[0] if args else "<no action>"
    stdin_text = json.dumps(stdin_json) if stdin_json is not None else None
    result = subprocess.run(
        [str(PYTHON), str(CLI_PATH), *args],
        cwd=str(REPO_ROOT),
        env={**os.environ, **profile_env},
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ActionError(
            f"cli.py action {action_name!r} produced non-JSON output "
            f"(exit {result.returncode}): stdout={result.stdout!r} stderr={result.stderr!r}"
        ) from exc

    if result.returncode != 0 and not allow_failure:
        raise ActionError(f"cli.py action {action_name!r} failed (exit {result.returncode}): {payload}")
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


@contextmanager
def dual_deployment(tmp_path, *, sync_enabled=True, start_node=True, keep_on_failure=False):
    """Stand up two real ephemeral PostgreSQL databases and (unless
    `start_node=False`, for scenarios that need Node offline/absent) two
    real Gunicorn processes, with a real issued Node sync credential
    already configured into the Node process's environment.

    Every generated artifact -- signing keys, .llpkg files, pg_dump
    files, Gunicorn logs, any other scenario temp artifact a caller
    places under `deployment.tmp_dir` -- lives inside ONE
    deployment-owned workspace directory created here, distinct from
    the caller-supplied `tmp_path` itself (which this function never
    deletes; it is the pytest fixture's own, not ours to remove).

    Tears everything down in `finally`, regardless of what the caller's
    `with` block does or how far setup got: stop processes -> remove
    the whole workspace recursively -> drop the Node database -> drop
    the Central database. Every one of those steps is attempted even if
    an earlier one fails (Node database cleanup failing must never skip
    Central database cleanup) -- run_cleanup_actions() collects every
    failure and raises one aggregated error only after all have been
    attempted.

    The protected cleanup scope (this function's own try/finally) begins
    BEFORE workspace creation and BEFORE either create_database() call --
    resource IDENTITIES (`run_id`/`gate_names`/`workspace` path) are only
    resolved/generated first, never created yet. This closes a partial-
    setup window where a real Central database could be created, then
    Node database creation fails, with no `finally` ever having started
    to protect the already-real Central database or the already-created
    workspace directory (Task 15 Tasks 7-10 partial-setup cleanup final
    corrective, Important). Every cleanup action below is idempotent for
    a resource that was never actually created, so registering cleanup
    for both database members and the workspace unconditionally is
    always safe.

    `keep_on_failure` (Task 15 Task 11, local-development-only debugging
    feature): when True AND the caller's `with` block actually raised
    (checked via `sys.exc_info()` at teardown time, not merely a flag
    the caller passed), the ephemeral Central/Node databases and the
    workspace directory are retained instead of dropped/removed, for
    local inspection. This NEVER applies on a successful run (the flag
    alone changes nothing unless the body actually failed), and it NEVER
    extends to the private signing key -- that is still deleted
    unconditionally in every case, exactly as already closed. Default
    (`keep_on_failure=False`) preserves the exact prior unconditional-
    cleanup behavior."""
    run_id = generate_run_id()
    gate_names = GateDatabaseNames.for_run(run_id)
    db_admin = db_admin_from_env()
    workspace = Path(tmp_path) / f"gate-workspace-{run_id}"

    central = None
    node = None
    deployment = None
    private_key_path = None
    try:
        workspace.mkdir(parents=True)
        create_database(db_admin, gate_names, "central")
        create_database(db_admin, gate_names, "node")

        central_profile_env = {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}
        node_profile_env_for_migrate = {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.node"}
        run_migrations(gate_names, "central", central_profile_env, repo_root=REPO_ROOT)
        run_migrations(gate_names, "node", node_profile_env_for_migrate, repo_root=REPO_ROOT)

        private_key_path, public_key_path = create_ephemeral_signing_keypair(workspace)

        central_listener = reserve_listener()
        central_profile_env = {
            "DJANGO_SETTINGS_MODULE": "linuxlab.settings.central",
            "POSTGRES_DB": gate_names.central,
        }
        central = start_gunicorn(central_profile_env, central_listener, workspace / "central.log")
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
            node = start_gunicorn(node_profile_env, node_listener, workspace / "node.log")
            wait_ready(node, timeout_seconds=READY_TIMEOUT_SECONDS)

        # Kept as a local reference (not just yielded) so `finally` below
        # can stop whatever process this object's .central/.node fields
        # point to AT TEARDOWN TIME -- a scenario that restarts Central
        # or Node (N4/N5) reassigns deployment.central/deployment.node on
        # this exact same object to the new ManagedProcess, and the
        # original pre-restart handles must never be the ones "stopped"
        # (they are usually already dead by then; stopping the CURRENT
        # live process is what actually matters).
        deployment = DualDeployment(
            run_id=run_id,
            gate_names=gate_names,
            db_admin=db_admin,
            tmp_dir=workspace,
            central=central,
            node=node,
            central_profile_env=central_profile_env,
            node_profile_env=node_profile_env,
            node_id=credential["node_id"],
            node_secret=credential["secret"],
            private_key_path=private_key_path,
            public_key_path=public_key_path,
        )
        yield deployment
    finally:
        current_node = deployment.node if deployment is not None else node
        current_central = deployment.central if deployment is not None else central
        # Reflects whether the caller's `with` block is currently
        # unwinding because of an exception -- never a caller-supplied
        # flag -- so a SUCCESSFUL run is always fully cleaned up even
        # when keep_on_failure=True was passed.
        body_failed = sys.exc_info()[0] is not None

        cleanup_actions = []
        if current_node is not None:
            cleanup_actions.append(("stop_node_process", lambda p=current_node: stop_process(p)))
        if current_central is not None:
            cleanup_actions.append(("stop_central_process", lambda p=current_central: stop_process(p)))

        if keep_on_failure and body_failed:
            # Local-debug-only: the ephemeral databases and the
            # workspace directory (the .llpkg, logs, etc.) are retained
            # for inspection. The private signing key is STILL deleted
            # unconditionally -- keep-on-failure never permits retaining
            # it, regardless of where inside the workspace it lives.
            if private_key_path is not None:
                cleanup_actions.append(
                    ("delete_private_key_only", lambda p=private_key_path: delete_private_key(p))
                )
        else:
            # Processes are stopped (and therefore their log file
            # handles closed) BEFORE the workspace those logs live in
            # is removed.
            cleanup_actions.append(("remove_workspace", lambda: remove_workspace(workspace)))
            # The hard invariant: Node database cleanup failing must
            # never skip Central database cleanup. Both are always
            # attempted.
            cleanup_actions.append(("drop_node_database", lambda: drop_database(db_admin, gate_names, "node")))
            cleanup_actions.append(
                ("drop_central_database", lambda: drop_database(db_admin, gate_names, "central"))
            )
        run_cleanup_actions(cleanup_actions)


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
        "--max-score",
        str(max_score),
        stdin_json={"correct_option_id": correct_option_id},
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
