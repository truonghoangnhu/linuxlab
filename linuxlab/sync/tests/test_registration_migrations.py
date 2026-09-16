"""Execute registration lifecycle history on disposable PostgreSQL databases."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest
from django.conf import settings
from django.db import IntegrityError, connections, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from linuxlab.sync.tests import _migration_db
from linuxlab.sync.tests._migration_db import scratch_database

BEFORE = [("sync", "0004_outboxmessage_claim_token")]
EXPANDED = [("sync", "0005_registration_lifecycle_schema")]
CLASSIFIED = [("sync", "0006_registration_lifecycle_data")]
AFTER = [("sync", "0007_registration_lifecycle_constraints")]


def scratch_inventory():
    with _migration_db._admin_connect() as admin, admin.cursor() as cursor:
        cursor.execute(
            "SELECT datname FROM pg_database WHERE starts_with(datname, %s)",
            ["test_migration_scratch_"],
        )
        return {row[0] for row in cursor.fetchall()}


@pytest.fixture(autouse=True)
def audit_resources():
    before = scratch_inventory()
    aliases = set(connections.databases)
    cached = {name for name in aliases if hasattr(connections._connections, name)}
    yield
    assert scratch_inventory() == before
    assert set(connections.databases) == aliases
    assert {name for name in aliases if hasattr(connections._connections, name)} == cached


@pytest.fixture
def scratch(django_db_blocker):
    with django_db_blocker.unblock(), scratch_database() as alias:
        yield alias
    assert alias not in connections.databases
    assert not hasattr(connections._connections, alias)


def migrate(alias, target):
    executor = MigrationExecutor(connections[alias])
    executor.migrate(target)
    return executor.loader.project_state(target).apps


def test_ambiguous_candidates_receive_no_active_authority(scratch):
    apps = migrate(scratch, BEFORE)
    registration = apps.get_model("sync", "NodeRegistration")
    node_id = uuid.uuid4()
    registration.objects.using(scratch).create(node_id=node_id, current_secret_hash="test-hash")
    apps = migrate(scratch, EXPANDED)
    registration = apps.get_model("sync", "NodeRegistration")
    registration.objects.using(scratch).create(node_id=node_id, current_secret_hash="test-hash-2")
    apps = migrate(scratch, AFTER)
    rows = apps.get_model("sync", "NodeRegistration").objects.using(scratch).filter(node_id=node_id)
    assert rows.count() == 2
    assert rows.filter(state="active").count() == 0
    assert set(rows.values_list("state", flat=True)) == {"revoked"}


def test_empty_inventory(scratch):
    migrate(scratch, BEFORE)
    apps = migrate(scratch, AFTER)
    assert apps.get_model("sync", "NodeRegistration").objects.using(scratch).count() == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_classification_preserves_all_legacy_evidence(scratch, reverse):
    at = datetime(2020, 1, 1, tzinfo=UTC)
    groups = [
        [(False, None, "revoked")],
        [(True, at, "revoked")],
        [(True, None, "active")],
        [(True, None, "active")],  # sole unusable hash still follows the mapping
        [(True, None, "active"), (False, None, "revoked"), (True, at, "revoked")],
        [(False, None, "revoked"), (True, at, "revoked")],
        [(True, None, "revoked"), (True, None, "revoked")],
        [
            (True, None, "revoked"),
            (True, None, "revoked"),
            (True, None, "revoked"),
            (False, at, "revoked"),
        ],
    ]
    apps = migrate(scratch, BEFORE)
    Registration = apps.get_model("sync", "NodeRegistration")
    Inbox = apps.get_model("sync", "InboxMessage")
    expected = {}
    pending = []
    for index, entries in enumerate(groups):
        node_id = uuid.uuid4()
        entries = list(reversed(entries)) if reverse else entries
        for offset, (enabled, revoked_at, state) in enumerate(entries):
            pk = 1000 - index * 20 - offset if reverse else 100 + index * 20 + offset
            fields = {
                "id": pk,
                "node_id": node_id,
                "display_name": f"legacy-{index}-{offset}",
                "enabled": enabled,
                "revoked_at": revoked_at,
                "current_secret_hash": "!" if index == 3 else "HASH_SENTINEL",
                "previous_secret_hash": "OLD_HASH_SENTINEL",
                "previous_valid_until": at + timedelta(days=index),
                "rotated_at": at - timedelta(days=index),
            }
            expected[pk] = state
            if offset:
                pending.append(fields)
                continue
            node = Registration.objects.using(scratch).create(**fields)
            Inbox.objects.using(scratch).create(
                event_id=uuid.uuid4(),
                source_node=node,
                event_type="activity.completed",
                schema_version=1,
                occurred_at=at,
                aggregate_type="attempt",
                aggregate_public_id=uuid.uuid4(),
                payload={"historical": index},
            )
    # Baseline uniqueness is real, so ambiguity is seeded only after expansion.
    with pytest.raises(IntegrityError), transaction.atomic(using=scratch):
        Registration.objects.using(scratch).create(node_id=node_id, current_secret_hash="duplicate")
    baseline = list(Registration.objects.using(scratch).order_by("pk").values())
    inbox_before = list(Inbox.objects.using(scratch).order_by("pk").values())
    apps = migrate(scratch, EXPANDED)
    Registration = apps.get_model("sync", "NodeRegistration")
    assert not Registration._meta.get_field("state").has_default()
    assert set(Registration.objects.using(scratch).values_list("state", flat=True)) == {None}
    for original in baseline:
        expanded = Registration.objects.using(scratch).values().get(pk=original["id"])
        expanded.pop("state")
        assert expanded == original
    for fields in pending:
        Registration.objects.using(scratch).create(**fields)
    rows = Registration.objects.using(scratch)
    for index, pk in enumerate(expected):
        rows.filter(pk=pk).update(created_at=at + timedelta(days=-index if reverse else index))
    legacy = list(rows.order_by("pk").values())
    with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no network")):
        apps = migrate(scratch, AFTER)
    Registration = apps.get_model("sync", "NodeRegistration")
    assert "public_id" not in {field.name for field in Registration._meta.fields}
    migrated = list(Registration.objects.using(scratch).order_by("pk").values())
    assert {row["id"]: row["state"] for row in migrated} == expected
    for before, after in zip(legacy, migrated, strict=True):
        before.pop("state")
        after.pop("state")
        assert after == before
    Inbox = apps.get_model("sync", "InboxMessage")
    assert list(Inbox.objects.using(scratch).order_by("pk").values()) == inbox_before
    for inbox in Inbox.objects.using(scratch).select_related("source_node"):
        original = next(row for row in legacy if row["id"] == inbox.source_node_id)
        assert inbox.source_node.node_id == original["node_id"]
    assert apps.get_model("sync", "OutboxMessage").objects.using(scratch).count() == 0
    assert apps.get_model("core", "DomainEvent").objects.using(scratch).count() == 0


def test_postgresql_lifecycle_constraints(scratch):
    apps = migrate(scratch, AFTER)
    Registration = apps.get_model("sync", "NodeRegistration")
    rows = Registration.objects.using(scratch)
    node_id = uuid.uuid4()
    rows.create(node_id=node_id, state="active", current_secret_hash="h")
    for state in ("revoked", "superseded", "revoked"):
        rows.create(node_id=node_id, state=state, current_secret_hash="h")
    rows.create(node_id=uuid.uuid4(), state="active", current_secret_hash="h")
    for state in ("active", "invalid", None):
        with pytest.raises(IntegrityError), transaction.atomic(using=scratch):
            rows.create(node_id=node_id, state=state, current_secret_hash="h")
    assert rows.count() == 5
    assert rows.create(current_secret_hash="h").state == "revoked"
    with connections[scratch].cursor() as cursor:
        constraints = connections[scratch].introspection.get_constraints(
            cursor, Registration._meta.db_table
        )
    assert constraints["node_registration_state_allowed_values"]["check"]
    assert constraints["unique_active_node_registration_per_node"]["unique"]
    assert any(
        item["index"] and item["columns"] == ["node_id"] and not item["unique"]
        for item in constraints.values()
    )


@pytest.mark.parametrize("bad_state", [None, "invalid", "duplicate_active"])
def test_final_verification_rejects_corrupt_intermediate_state(scratch, bad_state):
    apps = migrate(scratch, CLASSIFIED)
    Registration = apps.get_model("sync", "NodeRegistration")
    rows = Registration.objects.using(scratch)
    node_id = uuid.uuid4()
    rows.create(
        node_id=node_id,
        state="active" if bad_state == "duplicate_active" else bad_state,
        current_secret_hash="HASH_SENTINEL",
    )
    if bad_state == "duplicate_active":
        rows.create(node_id=node_id, state="active", current_secret_hash="HASH_SENTINEL")
    before = list(rows.order_by("pk").values())
    with pytest.raises(RuntimeError, match="Registration lifecycle verification failed") as error:
        migrate(scratch, AFTER)
    assert "SENTINEL" not in str(error.value)
    assert AFTER[0] not in MigrationRecorder(connections[scratch]).applied_migrations()
    assert list(rows.order_by("pk").values()) == before


def test_structural_reverse_is_distinct_from_irreversible_classification(scratch):
    apps = migrate(scratch, BEFORE)
    apps.get_model("sync", "NodeRegistration").objects.using(scratch).create(
        current_secret_hash="h"
    )
    apps = migrate(scratch, AFTER)
    before = list(apps.get_model("sync", "NodeRegistration").objects.using(scratch).values())
    apps = migrate(scratch, CLASSIFIED)
    Registration = apps.get_model("sync", "NodeRegistration")
    assert Registration._meta.get_field("state").null
    assert list(Registration.objects.using(scratch).values()) == before
    with pytest.raises(IrreversibleError):
        migrate(scratch, EXPANDED)
    recorder = MigrationRecorder(connections[scratch])
    assert CLASSIFIED[0] in recorder.applied_migrations()
    assert AFTER[0] not in recorder.applied_migrations()
    assert list(Registration.objects.using(scratch).values()) == before
    with connections[scratch].cursor() as cursor:
        columns = connections[scratch].introspection.get_table_description(
            cursor, Registration._meta.db_table
        )
        constraints = connections[scratch].introspection.get_constraints(
            cursor, Registration._meta.db_table
        )
    assert next(column for column in columns if column.name == "state").null_ok
    assert "unique_active_node_registration_per_node" not in constraints
    assert "node_registration_state_allowed_values" not in constraints


@pytest.mark.parametrize("failure", [None, "body", "setup", "close"])
def test_scratch_cleanup_covers_all_exit_paths(django_db_blocker, monkeypatch, failure):
    seen = {}
    original = _migration_db._configure_alias

    def configure(alias, name):
        seen.update(alias=alias, name=name)
        original(alias, name)
        if failure == "setup":
            raise RuntimeError("setup failure")
        if failure == "close":
            close = connections[alias].close

            def close_then_fail():
                close()
                raise RuntimeError("close failure")

            monkeypatch.setattr(connections[alias], "close", close_then_fail)

    monkeypatch.setattr(_migration_db, "_configure_alias", configure)

    def run():
        with django_db_blocker.unblock(), scratch_database() as alias:
            assert hasattr(connections._connections, alias)
            if failure == "body":
                raise RuntimeError("body failure")

    if failure:
        with pytest.raises(RuntimeError, match=failure + " failure"):
            run()
    else:
        run()
    assert seen["name"] not in scratch_inventory()
    assert seen["alias"] not in settings.DATABASES
    assert seen["alias"] not in connections.databases
    assert not hasattr(connections._connections, seen["alias"])


def test_failed_create_does_not_claim_or_drop_an_existing_database(django_db_blocker, monkeypatch):
    with django_db_blocker.unblock(), scratch_database() as alias:
        name = connections[alias].settings_dict["NAME"]
        # Deliberate collision with this test's own database, never an unknown DB.
        with monkeypatch.context() as collision:
            collision.setattr(
                _migration_db.uuid,
                "uuid4",
                lambda: SimpleNamespace(hex=name.removeprefix("test_migration_scratch_")),
            )
            with (
                patch.object(_migration_db, "_drop_database") as drop,
                pytest.raises(psycopg.errors.DuplicateDatabase),
                scratch_database(),
            ):
                pytest.fail("duplicate CREATE unexpectedly succeeded")
            drop.assert_not_called()
        assert name in scratch_inventory()
        with connections[alias].cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)


@pytest.mark.parametrize("body_failure", [False, True])
def test_cleanup_failure_is_visible_and_preserves_original_error(
    django_db_blocker, monkeypatch, body_failure
):
    drop = _migration_db._drop_database
    original = RuntimeError("original body failure")

    def drop_then_fail(name):
        drop(name)  # Real cleanup first: failure injection must not leak a DB.
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(_migration_db, "_drop_database", drop_then_fail)
    with (
        pytest.raises(RuntimeError, match="cleanup failure") as error,
        django_db_blocker.unblock(),
        scratch_database(),
    ):
        if body_failure:
            raise original
    if body_failure:
        assert error.value.__context__ is original
