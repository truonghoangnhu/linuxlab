import importlib
import json
import os
import subprocess
import sys

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.loader import MigrationLoader


def test_settings_profile_is_test():
    assert settings.LINUXLAB_PROFILE == "test"


def test_settings_use_postgresql_engine():
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"


# --- Central / Node profile identity and capability flags ------------------
#
# LINUXLAB_PROFILE and the LINUXLAB_CAN_* flags are hardcoded per module (no
# environment lookup), so a plain, cached import is deterministic and safe.


def test_central_profile_identity():
    central = importlib.import_module("linuxlab.settings.central")
    assert central.LINUXLAB_PROFILE == "central"


def test_node_profile_identity():
    node = importlib.import_module("linuxlab.settings.node")
    assert node.LINUXLAB_PROFILE == "node"


def test_central_capability_flags():
    central = importlib.import_module("linuxlab.settings.central")
    assert central.LINUXLAB_CAN_AUTHOR is True
    assert central.LINUXLAB_CAN_IMPORT_PACKAGES is False


def test_node_capability_flags():
    node = importlib.import_module("linuxlab.settings.node")
    assert node.LINUXLAB_CAN_AUTHOR is False
    assert node.LINUXLAB_CAN_IMPORT_PACKAGES is True


def test_central_sync_capability_flags():
    central = importlib.import_module("linuxlab.settings.central")
    assert central.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS is True
    assert central.LINUXLAB_CAN_RUN_SYNC_WORKER is False


def test_node_sync_capability_flags():
    node = importlib.import_module("linuxlab.settings.node")
    assert node.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS is False
    assert node.LINUXLAB_CAN_RUN_SYNC_WORKER is True


# --- Deployment database name defaults --------------------------------------
#
# database_config() reads POSTGRES_DB from the environment at import time,
# and Python only runs a module's top-level code on its first import per
# process. Checking the *default* in-process would therefore silently
# depend on whichever POSTGRES_DB (if any) happened to be set the first
# time a settings module was touched in this pytest session. Each profile
# is instead loaded in a fresh subprocess with POSTGRES_DB stripped, so the
# default is observed deterministically regardless of ambient shell state
# or import order.


def _database_name(module_name):
    code = (
        "import importlib, json\n"
        f"m = importlib.import_module({module_name!r})\n"
        "print(json.dumps(m.DATABASES['default']['NAME']))\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "POSTGRES_DB"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_central_database_defaults_to_linuxlab_central_dev():
    assert _database_name("linuxlab.settings.central") == "linuxlab_central_dev"


def test_node_database_defaults_to_linuxlab_node_dev():
    assert _database_name("linuxlab.settings.node") == "linuxlab_node_dev"


def test_test_database_defaults_to_linuxlab_test():
    assert _database_name("linuxlab.settings.test") == "linuxlab_test"


def test_central_node_test_databases_are_mutually_distinct():
    names = {
        _database_name("linuxlab.settings.central"),
        _database_name("linuxlab.settings.node"),
        _database_name("linuxlab.settings.test"),
    }
    assert len(names) == 3


# --- Custom user model bootstrap --------------------------------------------


def test_auth_user_model_setting():
    assert settings.AUTH_USER_MODEL == "identity.Account"


def test_get_user_model_resolves_to_account():
    Account = get_user_model()
    assert Account._meta.label == "identity.Account"


# --- Identity migration graph ------------------------------------------------


@pytest.mark.django_db
def test_identity_initial_migration_is_recorded_and_applied():
    loader = MigrationLoader(connection)
    assert ("identity", "0001_initial") in loader.disk_migrations
    assert ("identity", "0001_initial") in loader.graph.nodes
    assert ("identity", "0001_initial") in loader.applied_migrations


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("profile", ["central", "node"])
@pytest.mark.parametrize(
    "bad_setting",
    [
        "LINUXLAB_SYNC_BACKOFF_BASE_SECONDS",
        "LINUXLAB_SYNC_BACKOFF_CAP_SECONDS",
        "LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS",
        "LINUXLAB_SYNC_RETRY_MAX_AGE",
        "LINUXLAB_SYNC_BATCH_MAX_EVENTS",
        "LINUXLAB_SYNC_BATCH_MAX_BYTES",
        "LINUXLAB_SYNC_LEASE_SECONDS",
        "LINUXLAB_SYNC_ENABLED",
        "LINUXLAB_SYNC_ALLOW_INSECURE_HTTP",
        "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE",
    ],
)
def test_malformed_sync_environment_preserves_local_readiness(profile, bad_setting):
    # Pin every database component to pytest's live, migrated database.
    env = dict(os.environ)
    for name in list(env):
        if (
            name.startswith("LINUXLAB_SYNC_")
            or name == "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE"
        ):
            del env[name]
    for name in ("NAME", "USER", "PASSWORD", "HOST", "PORT"):
        env["POSTGRES_DB" if name == "NAME" else "POSTGRES_" + name] = str(
            connection.settings_dict[name]
        )
    env.update(
        DJANGO_SETTINGS_MODULE=f"linuxlab.settings.{profile}",
        DJANGO_ALLOWED_HOSTS="testserver,localhost",
        LINUXLAB_SYNC_ENABLED="true",
        LINUXLAB_NODE_ID="b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10",
        LINUXLAB_SYNC_SECRET="SECRET_SENTINEL",
        LINUXLAB_CENTRAL_URL="https://central.example.org",
    )
    env[bad_setting] = "REJECTED_SENTINEL"
    code = """
import io, json, sys
from unittest.mock import patch
import django
django.setup()
from django.conf import settings
from django.test import Client
from django.core.management import call_command
from django.core.management.base import CommandError
from linuxlab.sync.configuration import load_sync_configuration, load_inbound_limits, SyncConfigurationError
from linuxlab.sync.models import OutboxMessage, InboxMessage, NodeRegistration
from linuxlab.core.models import DomainEvent
models = (OutboxMessage, InboxMessage, NodeRegistration, DomainEvent)
def snapshot():
    return {model: list(model.objects.order_by("pk").values()) for model in models}
before = snapshot()
with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no Central HTTP")):
    for path in ("/health/", "/healthz", "/ready/", "/readyz"):
        response = Client().get(path)
        assert response.status_code == 200 and response.json() == {"status":"ok"}
    name = sys.argv[1]
    if "BATCH_MAX" in name:
        load = load_inbound_limits
    elif "BACKOFF" in name or "RETRY" in name:
        load = lambda: load_sync_configuration(purpose="retry")
    else:
        load = lambda: load_sync_configuration(purpose="release")
    relevant = settings.LINUXLAB_PROFILE == "node" or "BATCH_MAX" in name or "BACKOFF" in name or "RETRY" in name
    try:
        load()
    except SyncConfigurationError as exc:
        assert relevant
        assert "SENTINEL" not in str(exc) + repr(exc)
        outcome = "rejected"
    else:
        assert not relevant
        outcome = "irrelevant"
    with patch("linuxlab.sync.management.commands.sync_events.HttpSyncTransport") as transport, patch("linuxlab.sync.management.commands.sync_events.flush_outbox") as flush:
        # The release-only flag does not gate the worker; all other Node values do.
        if name != "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE" or settings.LINUXLAB_PROFILE == "central":
            try:
                call_command("sync_events", "--once", stdout=io.StringIO(), stderr=io.StringIO())
            except CommandError as exc:
                assert "SENTINEL" not in str(exc)
            else:
                raise AssertionError("worker accepted invalid configuration")
            transport.assert_not_called()
            flush.assert_not_called()
assert snapshot() == before
print(json.dumps({"startup":"ok", "readiness":"ok", "capability":outcome}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, bad_setting],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "SENTINEL" not in result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["startup"] == report["readiness"] == "ok"
    assert report["capability"] in ("rejected", "irrelevant")
