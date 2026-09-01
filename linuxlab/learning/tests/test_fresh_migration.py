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

T15-D27 fresh-migration final corrective (two Important findings closed
here):

1. Schema verification previously checked only that a constraint NAME
   existed (`_constraint_exists()`), which a same-named but semantically
   different constraint would satisfy just as well -- confirmed
   empirically: dropping the real XOR CheckConstraint and replacing it
   with `CHECK (TRUE)` under the identical name still passed the old
   check. Verification now compares the constraint's exact
   `pg_get_constraintdef()` text (see
   test_exact_xor_verifier_rejects_a_same_named_but_semantically_wrong_constraint).

2. Cleanup previously issued a single `DROP DATABASE ... WITH (FORCE)`,
   which requires terminate-backend privileges the ordinary `linuxlab`
   application role is not guaranteed to hold over every connection that
   might transiently remain (e.g. from a different role/session) --
   verified empirically that this role is NOT superuser
   (`pg_roles.rolsuper = false`). Cleanup now polls pg_stat_activity for
   this scratch database's other active connections and waits, bounded
   by a short deterministic interval and an overall deadline, for them
   to close on their own before issuing a plain `DROP DATABASE` -- never
   `pg_terminate_backend()`, never `WITH (FORCE)` as the primary
   mechanism (see test_cleanup_waits_for_a_transient_connection_before_dropping).
"""

import contextlib
import os
import subprocess
import threading
import time
import uuid

import psycopg
import pytest
from django.conf import settings

_REPO_ROOT = str(settings.BASE_DIR)
_PYTHON = os.path.join(_REPO_ROOT, ".venv", "bin", "python")

_CLEANUP_POLL_INTERVAL_SECONDS = 0.2
_CLEANUP_DEADLINE_SECONDS = 15.0

_ATTEMPT_XOR_CONSTRAINT_NAME = "attempt_exactly_one_execution_source"
# The exact, deterministic text PostgreSQL's own pg_get_constraintdef()
# returns for this CHECK constraint (confirmed empirically against a
# freshly migrated database) -- Postgres normalizes parenthesization and
# operator spacing itself, so an exact-text comparison (after collapsing
# only incidental whitespace) is sufficient without any SQL parsing here.
_ATTEMPT_XOR_CONSTRAINT_DEFINITION = (
    "CHECK ((((activity_version_id IS NOT NULL) AND (installed_activity_version_id IS NULL)) "
    "OR ((activity_version_id IS NULL) AND (installed_activity_version_id IS NOT NULL))))"
)

_PROGRESS_UNIQUE_CONSTRAINT_NAME = "unique_progress_per_learner_activity_public_id"
_PROGRESS_UNIQUE_CONSTRAINT_DEFINITION = "UNIQUE (learner_id, activity_definition_public_id)"


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


def _active_connection_count(cursor, db_name):
    """Every connection to `db_name` other than this admin connection
    itself (pg_backend_pid() excludes the caller, so a lingering
    migration-subprocess or test-probe connection is what's actually
    counted, never this polling connection)."""
    cursor.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
        [db_name],
    )
    return cursor.fetchone()[0]


def _drop_scratch_database(
    name,
    *,
    poll_interval=_CLEANUP_POLL_INTERVAL_SECONDS,
    deadline_seconds=_CLEANUP_DEADLINE_SECONDS,
    on_wait=None,
):
    """Bounded, privilege-safe cleanup (T15-D27 fresh-migration final
    corrective): never depends on `DROP DATABASE ... WITH (FORCE)` or
    `pg_terminate_backend()` -- both require terminate privileges the
    ordinary `linuxlab` application role is not guaranteed to hold over
    connections it does not own. Instead, polls pg_stat_activity for this
    scratch database's other active connections and waits, bounded by a
    short deterministic interval and an overall deadline, for them to
    close on their own -- exactly what a short-lived migration subprocess
    connection does when it exits -- before issuing a plain
    `DROP DATABASE`.

    Safe to call when the database is already absent (a no-op). Never
    silently swallows a cleanup failure: exceeding the deadline raises
    TimeoutError with sanitized diagnostic metadata (database name,
    remaining connection count) -- never connection strings, credentials,
    or query text.

    `on_wait(remaining)`, if given, is called once per poll iteration
    that observes at least one other active connection -- test-only
    observability, never required for correctness. Returns the number of
    such iterations, so a test can assert genuine waiting occurred rather
    than an immediate lucky drop.
    """
    deadline = time.monotonic() + deadline_seconds
    waited_iterations = 0

    with _admin_connect() as conn:
        while True:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
                if cursor.fetchone() is None:
                    return waited_iterations

                remaining = _active_connection_count(cursor, name)

            if remaining == 0:
                break

            waited_iterations += 1
            if on_wait is not None:
                on_wait(remaining)

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"cleanup deadline ({deadline_seconds}s) exceeded waiting for "
                    f"{remaining} active connection(s) on scratch database {name!r} to close"
                )
            time.sleep(poll_interval)

        with conn.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')

    return waited_iterations


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


def _normalize_constraint_definition(definition):
    """Collapse only insignificant whitespace -- pg_get_constraintdef()
    itself already normalizes parenthesization/operator spacing
    deterministically for a given constraint body, so this never needs to
    understand SQL structure, only avoid failing on incidental whitespace
    differences."""
    return " ".join(definition.split())


def _exact_constraint_definition(cursor, table_name, constraint_name):
    """The exact PostgreSQL-normalized definition text for one named
    constraint on `table_name`, or None if no such constraint exists."""
    cursor.execute(
        """
        SELECT pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = %s AND c.conname = %s
        """,
        [table_name, constraint_name],
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _assert_exact_constraint_definition(cursor, table_name, constraint_name, expected_definition):
    """Proves the constraint's actual semantics, not merely that some
    constraint with this name exists (T15-D27 fresh-migration final
    corrective): a same-named but semantically different constraint --
    e.g. `CHECK (TRUE)` in place of the real XOR -- must fail this
    assertion. See
    test_exact_xor_verifier_rejects_a_same_named_but_semantically_wrong_constraint.
    """
    actual = _exact_constraint_definition(cursor, table_name, constraint_name)
    assert actual is not None, f"constraint {constraint_name!r} does not exist on {table_name!r}"
    assert _normalize_constraint_definition(actual) == _normalize_constraint_definition(
        expected_definition
    ), (
        f"constraint {constraint_name!r} on {table_name!r} has unexpected definition:\n"
        f"  actual:   {actual!r}\n"
        f"  expected: {expected_definition!r}"
    )


def _assert_t15d27_schema(db_name):
    with psycopg.connect(**_connection_params(db_name)) as conn, conn.cursor() as cursor:
        assert _column_exists(cursor, "learning_attempt", "activity_version_id")
        assert _column_exists(cursor, "learning_attempt", "installed_activity_version_id")
        assert _column_exists(cursor, "learning_progress", "activity_definition_public_id")

        _assert_exact_constraint_definition(
            cursor,
            "learning_attempt",
            _ATTEMPT_XOR_CONSTRAINT_NAME,
            _ATTEMPT_XOR_CONSTRAINT_DEFINITION,
        )
        _assert_exact_constraint_definition(
            cursor,
            "learning_progress",
            _PROGRESS_UNIQUE_CONSTRAINT_NAME,
            _PROGRESS_UNIQUE_CONSTRAINT_DEFINITION,
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


# --- exact XOR semantics, not name-only (T15-D27 fresh-migration final corrective) ---


def test_exact_xor_verifier_rejects_a_same_named_but_semantically_wrong_constraint():
    """Reproduces the exact defect the independent review found: drop the
    real XOR CheckConstraint and recreate a constraint with the IDENTICAL
    NAME but `CHECK (TRUE)` semantics. A name-only existence check would
    wrongly accept this (confirmed empirically before this corrective);
    _assert_exact_constraint_definition() must reject it."""
    db_name = f"test_fresh_xornegative_{uuid.uuid4().hex[:8]}"
    _create_scratch_database(db_name)
    try:
        result = _run_full_migrate("linuxlab.settings.central", db_name)
        assert result.returncode == 0, result.stdout + result.stderr

        with psycopg.connect(**_connection_params(db_name)) as conn, conn.cursor() as cursor:
            cursor.execute(
                f"ALTER TABLE learning_attempt DROP CONSTRAINT {_ATTEMPT_XOR_CONSTRAINT_NAME}"
            )
            cursor.execute(
                f"ALTER TABLE learning_attempt ADD CONSTRAINT {_ATTEMPT_XOR_CONSTRAINT_NAME} "
                "CHECK (TRUE)"
            )
            conn.commit()

        with (
            psycopg.connect(**_connection_params(db_name)) as conn,
            conn.cursor() as cursor,
            pytest.raises(AssertionError, match="unexpected definition"),
        ):
            _assert_exact_constraint_definition(
                cursor,
                "learning_attempt",
                _ATTEMPT_XOR_CONSTRAINT_NAME,
                _ATTEMPT_XOR_CONSTRAINT_DEFINITION,
            )
    finally:
        _drop_scratch_database(db_name)


# --- cleanup without terminate/FORCE privilege (T15-D27 fresh-migration final corrective) ---


def test_cleanup_waits_for_a_transient_connection_before_dropping():
    """A genuine second connection to the scratch database (simulating a
    slow-to-close migration-subprocess connection) must make cleanup
    WAIT, not fail and not silently proceed -- proven deterministically
    via an on_wait observability hook plus wall-clock ordering, not
    merely an eventual successful drop (which an unrelated race could
    also produce)."""
    db_name = f"test_fresh_cleanupwait_{uuid.uuid4().hex[:8]}"
    _create_scratch_database(db_name)

    holder = psycopg.connect(**_connection_params(db_name))
    try:
        with _admin_connect() as conn, conn.cursor() as cursor:
            # >= 1, not == 1: PostgreSQL may transiently attach an
            # unrelated background backend (e.g. an autovacuum worker) to
            # a freshly created database around the same moment -- a real
            # possibility this test must tolerate, not a defect in
            # `holder`'s own connection. The precondition only needs to
            # confirm at least one genuine connection exists to wait on;
            # the actual proof of correct blocking behavior is the
            # on_wait()/wall-clock assertions below, not this count.
            assert _active_connection_count(cursor, db_name) >= 1

        observed_waiting = threading.Event()
        results = {}

        def _on_wait(remaining):
            del remaining
            observed_waiting.set()

        def _run_cleanup():
            results["waited_iterations"] = _drop_scratch_database(db_name, on_wait=_on_wait)
            results["dropped_at"] = time.monotonic()

        cleanup_thread = threading.Thread(target=_run_cleanup)
        cleanup_thread.start()

        assert observed_waiting.wait(timeout=5), "cleanup never observed the transient connection"
        close_time = time.monotonic()
        holder.close()

        cleanup_thread.join(timeout=10)
        assert not cleanup_thread.is_alive(), "cleanup did not finish after the connection closed"

        assert results.get("waited_iterations", 0) >= 1, "cleanup never actually waited"
        assert results["dropped_at"] >= close_time, "database was dropped before the connection closed"

        with _admin_connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [db_name])
            assert cursor.fetchone() is None, "scratch database was not actually dropped"
    finally:
        # `holder` is already closed by the test body itself in the
        # normal path; psycopg raises on a double-close, which this
        # cleanup must not turn into a spurious test failure.
        with contextlib.suppress(Exception):
            holder.close()
        _drop_scratch_database(db_name)


def test_cleanup_is_a_no_op_when_the_database_is_already_absent():
    db_name = f"test_fresh_alreadygone_{uuid.uuid4().hex[:8]}"
    # Never created at all -- cleanup must still succeed cleanly.
    waited_iterations = _drop_scratch_database(db_name)
    assert waited_iterations == 0


def test_cleanup_runs_even_when_the_migration_subprocess_fails():
    """A permanent regression proof (not just a manual check) that the
    scratch database created for a fresh-migration proof is dropped even
    when the migration subprocess itself fails -- the `finally` block in
    _run_fresh_full_migration_proof() must execute cleanup regardless of
    where inside `try` the failure occurs. Uses an intentionally-invalid
    settings module so `manage.py migrate` fails deterministically
    without ever reaching a real migration."""
    db_name = f"test_fresh_failpath_{uuid.uuid4().hex[:8]}"
    with pytest.raises(AssertionError, match="manage.py migrate --noinput failed"):
        _run_broken_migration_proof(db_name)

    with _admin_connect() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [db_name])
        assert cursor.fetchone() is None, "scratch database leaked after a failed migration"


def _run_broken_migration_proof(db_name):
    """Identical shape to _run_fresh_full_migration_proof(), pointed at a
    settings module that does not exist, so `manage.py migrate` itself
    fails and the surrounding assertion (and therefore the `finally`
    cleanup) is exercised the same way a genuine migration failure would
    exercise it."""
    _create_scratch_database(db_name)
    try:
        result = _run_full_migrate("linuxlab.settings.does_not_exist", db_name)
        assert result.returncode == 0, (
            f"manage.py migrate --noinput failed (exit {result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        _assert_t15d27_schema(db_name)
    finally:
        _drop_scratch_database(db_name)
