"""T15-D27 corrective (Task 5, "fresh migration tests"): the `learning`
app's migration graph -- including 0002_t15d27_installed_execution_source
and its transitive activities/identity/sync dependencies -- applies
cleanly to a genuinely empty PostgreSQL database (no legacy data, never
touching the shared "default" test database), and the resulting schema
physically has the XOR CheckConstraint PostgreSQL itself will enforce.

Central and Node share one codebase and one migration graph (ADR-002,
ADR-008): INSTALLED_APPS is not profile-conditional, so there is exactly
one schema to prove here, not two -- a Central-profile and a Node-profile
deployment migrating from zero apply the identical set of migrations to
an identical resulting schema; only runtime settings values (e.g.
LINUXLAB_CAN_AUTHOR) differ between profiles, never the schema itself.

Deliberately targets only `learning`'s own leaf node (via MigrationExecutor,
not `manage.py migrate`'s full-INSTALLED_APPS sweep): learning's migrations
transitively depend on activities/identity/sync only, never on
content/wagtailcore, so this never needs to run Wagtail's own migrations
or post_migrate signal handlers at all.
"""

import uuid

import psycopg
import pytest
from django.conf import settings
from django.db import connections
from django.db.migrations.executor import MigrationExecutor


def _admin_connect():
    db = settings.DATABASES["default"]
    return psycopg.connect(
        dbname="postgres",
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"],
        autocommit=True,
    )


def _constraint_exists(alias, table_name, constraint_name):
    with connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass",
            [constraint_name, table_name],
        )
        return cursor.fetchone() is not None


@pytest.mark.django_db(transaction=True)
def test_fresh_database_migrates_cleanly_with_the_t15d27_constraint_present(django_db_blocker):
    name = f"test_fresh_migration_{uuid.uuid4().hex[:8]}"
    alias = f"fresh_migration_{uuid.uuid4().hex[:8]}"

    with _admin_connect() as admin_conn, admin_conn.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{name}"')

    settings.DATABASES[alias] = {**settings.DATABASES["default"], "NAME": name}

    try:
        with django_db_blocker.unblock():
            executor = MigrationExecutor(connections[alias])
            # The real "learning" migration graph, from zero, exactly as
            # an operator deploying a brand-new Central or Node instance
            # would apply it -- never a hand-picked migration subset.
            executor.migrate(executor.loader.graph.leaf_nodes("learning"))

            assert _constraint_exists(
                alias, "learning_attempt", "attempt_exactly_one_execution_source"
            )
            assert _constraint_exists(
                alias, "learning_progress", "unique_progress_per_learner_activity_public_id"
            )
    finally:
        connections[alias].close()
        del connections[alias]
        del connections.databases[alias]
        with _admin_connect() as admin_conn, admin_conn.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
