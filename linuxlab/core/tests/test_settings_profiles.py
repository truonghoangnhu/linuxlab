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
