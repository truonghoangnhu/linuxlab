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
from scripts.release_gate.signing_material import create_ephemeral_signing_keypair

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
def dual_deployment(tmp_path, *, sync_enabled=True, start_node=True, run_id=None, resource_ledger=None):
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

    Tears everything down in `finally`, UNCONDITIONALLY -- regardless of
    what the caller's `with` block does, how far setup got, or whether
    it succeeded or failed: stop processes -> remove the whole workspace
    recursively -> drop the Node database -> drop the Central database.
    Every one of those steps is attempted even if an earlier one fails
    (Node database cleanup failing must never skip Central database
    cleanup) -- run_cleanup_actions() collects every failure and raises
    one aggregated error only after all have been attempted.

    Task 15 Task 11 corrective v2, Section 3 ("execution resources are
    never retained"): there is deliberately no retention branch and no
    `keep_on_failure` parameter here any more. Ephemeral Central/Node
    databases, the workspace directory (`.llpkg`, logs, dumps, keys),
    and the private signing key are ALL destroyed on every exit path,
    success or failure alike. The only place a failed run's diagnostic
    content may persist is the sanitized evidence pair gate_runner
    itself writes -- never raw execution state such as a real database
    or a workspace that could contain answer-bearing package content or
    server-side grading state.

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

    `run_id` (Task 15 Task 11 corrective): a caller that already owns a
    run identity (gate_runner's RunContext) may supply a deterministic
    CHILD run_id here instead of letting this function mint a
    completely unrelated random one -- so every one of this scenario's
    own ephemeral resources is traceably derived from the SAME outer
    run for the duration it exists, even though none of it survives
    past this function's own `finally`. Defaults to a freshly generated
    run_id, preserving the exact prior behavior when omitted.

    `resource_ledger` (Task 15 Task 11 corrective v9, Section 11-16): an
    optional shared `gate_runner.ResourceLedger` -- when given, this
    function acquires ownership of the workspace directory and BOTH
    ephemeral databases the INSTANT each physical creation succeeds
    (never retroactively inferred later from a cleanup-exception's
    action name), and each cleanup action below releases its OWN
    ledger entry immediately after its OWN physical disposal action
    verifiably succeeds -- release is directly coupled to that exact
    resource's own outcome, never to string-matching a cleanup action's
    name against a table. Defaults to None, preserving the exact prior
    behavior (no ledger involvement at all) for every existing caller
    that does not pass one -- also set as `deployment.resource_ledger`
    on the yielded object, since scenario code (e.g.
    scenarios.run_backup_restore_smoke()) reads it from there."""
    run_id = run_id if run_id is not None else generate_run_id()
    gate_names = GateDatabaseNames.for_run(run_id)
    db_admin = db_admin_from_env()
    workspace = Path(tmp_path) / f"gate-workspace-{run_id}"

    central = None
    node = None
    deployment = None
    private_key_path = None
    try:
        workspace.mkdir(parents=True)
        if resource_ledger is not None:
            resource_ledger.acquire("workspace", str(workspace))

        create_database(db_admin, gate_names, "central")
        if resource_ledger is not None:
            resource_ledger.acquire("central_db", gate_names.central)

        create_database(db_admin, gate_names, "node")
        if resource_ledger is not None:
            resource_ledger.acquire("node_db", gate_names.node)

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
        # Task 15 Task 11 corrective v9, Section 13: set directly on the
        # yielded object here (not left to be assigned later by a
        # caller such as gate_runner._run_one_scenario(), which used to
        # be the ONLY place this got set, well AFTER workspace/database
        # creation had already happened above) -- scenario code (e.g.
        # scenarios.run_backup_restore_smoke()) reads
        # `deployment.resource_ledger` unconditionally via getattr().
        deployment.resource_ledger = resource_ledger
        yield deployment
    finally:
        current_node = deployment.node if deployment is not None else node
        current_central = deployment.central if deployment is not None else central

        cleanup_actions = []
        if current_node is not None:
            cleanup_actions.append(("stop_node_process", lambda p=current_node: stop_process(p)))
        if current_central is not None:
            cleanup_actions.append(("stop_central_process", lambda p=current_central: stop_process(p)))
        # Processes are stopped (and therefore their log file handles
        # closed) BEFORE the workspace those logs live in is removed.
        # Every execution resource -- workspace, both ephemeral
        # databases -- is destroyed unconditionally, success or
        # failure alike (Task 15 Task 11 corrective v2, Section 3).
        #
        # Task 15 Task 11 corrective v9, Section 20/23: each action
        # below releases its OWN ledger entry immediately after ITS OWN
        # physical disposal action verifiably succeeds (the lambda body
        # only reaches the `resource_ledger.release(...)` line if the
        # preceding disposal call returned without raising) -- release
        # is directly coupled to verified success of that exact
        # resource's own disposal, with ZERO dependence on matching a
        # cleanup action's name against any table. A resource that was
        # never actually created (e.g. Node database creation itself
        # failed) is simply never in the ledger to begin with, so
        # releasing it here is always a safe no-op (ResourceLedger.
        # release() on an absent entry is a no-op `set.discard()`).
        def _remove_workspace_and_release():
            remove_workspace(workspace)
            if resource_ledger is not None:
                resource_ledger.release("workspace", str(workspace))

        def _drop_node_database_and_release():
            drop_database(db_admin, gate_names, "node")
            if resource_ledger is not None:
                resource_ledger.release("node_db", gate_names.node)

        def _drop_central_database_and_release():
            drop_database(db_admin, gate_names, "central")
            if resource_ledger is not None:
                resource_ledger.release("central_db", gate_names.central)

        cleanup_actions.append(("remove_workspace", _remove_workspace_and_release))
        # The hard invariant: Node database cleanup failing must never
        # skip Central database cleanup. Both are always attempted.
        cleanup_actions.append(("drop_node_database", _drop_node_database_and_release))
        cleanup_actions.append(("drop_central_database", _drop_central_database_and_release))
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
