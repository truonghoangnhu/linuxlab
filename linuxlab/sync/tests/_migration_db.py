"""Owned PostgreSQL scratch databases for irreversible migration tests only."""

import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

import psycopg
from django.conf import settings
from django.db import connections
from psycopg import sql


def _admin_connect():
    db = settings.DATABASES["default"]
    return psycopg.connect(
        dbname="postgres",
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"],
        autocommit=True,
        connect_timeout=5,
        options="-c statement_timeout=10000 -c lock_timeout=5000",
    )


def _drop_database(name):
    with _admin_connect() as admin, admin.cursor() as cursor:
        cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def _remove_alias(alias):
    try:
        if hasattr(connections._connections, alias):
            connections[alias].close()
    finally:
        if hasattr(connections._connections, alias):
            del connections[alias]
        connections.databases.pop(alias, None)
        settings.DATABASES.pop(alias, None)


def _configure_alias(alias, name):
    settings.DATABASES[alias] = {**settings.DATABASES["default"], "NAME": name}
    connections[alias].ensure_connection()


@contextmanager
def scratch_database() -> Iterator[str]:
    name = f"test_migration_scratch_{uuid.uuid4().hex}"
    alias = f"migration_scratch_{uuid.uuid4().hex}"
    # ExitStack runs all cleanup even if a prior callback fails, preserving
    # exception context. Register ownership only after successful CREATE.
    with ExitStack() as cleanup:
        with _admin_connect() as admin, admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            cleanup.callback(_drop_database, name)
        cleanup.callback(_remove_alias, alias)
        _configure_alias(alias, name)
        yield alias
