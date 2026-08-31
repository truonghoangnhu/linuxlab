"""T15-D27 final corrective (fresh-migration finding): the FULL migration
graph -- every INSTALLED_APPS app, including wagtailcore and every other
app the leaf-only proof deliberately skipped -- applies cleanly, under
BOTH the real Central and the real Node deployment profile, exactly as an
operator running `manage.py migrate --noinput` on a brand-new deployment
would apply it.

Deliberately invokes `manage.py migrate` as a genuine separate
subprocess (never Django's in-process MigrationExecutor/call_command
against an alias registered on this already-running pytest process): an
in-process multi-database migrate was found to trip a Wagtail
post_migrate signal-routing quirk unrelated to any real migration defect
-- confirmed by running the identical `manage.py migrate --noinput`
command as a real subprocess against the same scratch database, which
succeeds cleanly. Using a real subprocess is both the fix for that false
signal and a more faithful proof of the actual operator workflow.

Each test creates and drops its own uniquely-named scratch PostgreSQL
database via direct psycopg admin connections -- never
MigrationExecutor, never call_command -- so the shared pytest test
database's schema is never at any risk of being touched, let alone
rolled backward. Central and Node share one INSTALLED_APPS (ADR-002,
ADR-008), so this is the same full graph proven twice, once per real
settings module, not two different graphs.
"""

import os
import subprocess
import uuid

import psycopg
from django.conf import settings

_REPO_ROOT = str(settings.BASE_DIR)
_PYTHON = os.path.join(_REPO_ROOT, ".venv", "bin", "python")


def _connection_params(db_name):
    db = settings.DATABASES["default"]
    return {
        "user": db["USER"],
        "password": db["PASSWORD"],
        "host": db["HOST"],
        "port": db["PORT"],
        "dbname": db_name,
    }


def _admin_connect():
    return psycopg.connect(**_connection_params("postgres"), autocommit=True)


def _create_scratch_database(name):
    with _admin_connect() as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')


def _drop_scratch_database(name):
    with _admin_connect() as conn, conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _run_full_migrate(settings_module, db_name):
    """A real, separate `manage.py migrate --noinput` process -- exactly
    the command an operator deploying a brand-new Central or Node
    instance would run, never an in-process shortcut."""
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": settings_module, "POSTGRES_DB": db_name}
    return subprocess.run(
        [_PYTHON, "manage.py", "migrate", "--noinput"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _column_exists(cursor, table_name, column_name):
    cursor.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        [table_name, column_name],
    )
    return cursor.fetchone() is not None


def _constraint_exists(cursor, table_name, constraint_name):
    cursor.execute(
        "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass",
        [constraint_name, table_name],
    )
    return cursor.fetchone() is not None


def _assert_t15d27_schema(db_name):
    with psycopg.connect(**_connection_params(db_name)) as conn, conn.cursor() as cursor:
        assert _column_exists(cursor, "learning_attempt", "activity_version_id")
        assert _column_exists(cursor, "learning_attempt", "installed_activity_version_id")
        assert _column_exists(cursor, "learning_progress", "activity_definition_public_id")

        assert _constraint_exists(
            cursor, "learning_attempt", "attempt_exactly_one_execution_source"
        )
        assert _constraint_exists(
            cursor, "learning_progress", "unique_progress_per_learner_activity_public_id"
        )


def _run_fresh_full_migration_proof(settings_module, db_prefix):
    db_name = f"test_fresh_{db_prefix}_{uuid.uuid4().hex[:8]}"
    _create_scratch_database(db_name)
    try:
        result = _run_full_migrate(settings_module, db_name)
        assert result.returncode == 0, (
            f"manage.py migrate --noinput failed (exit {result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        _assert_t15d27_schema(db_name)
    finally:
        _drop_scratch_database(db_name)


def test_fresh_central_migration_reaches_leaf_with_t15d27_constraints():
    _run_fresh_full_migration_proof("linuxlab.settings.central", "central")


def test_fresh_node_migration_reaches_leaf_with_t15d27_constraints():
    _run_fresh_full_migration_proof("linuxlab.settings.node", "node")
