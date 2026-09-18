"""Execute Outbox retry-state schema/data migration history on disposable
PostgreSQL databases (Corrective B Task 7). `first_failure_at` is a
conservative UPPER-BOUND AGE ANCHOR seeded from legacy `created_at` for
already-retrying rows -- never the actual historical first-failure
instant, never a fresh budget, never migration-execution time."""

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.db import IntegrityError, connections, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from linuxlab.sync.tests import _migration_db
from linuxlab.sync.tests._migration_db import scratch_database

BEFORE = [("sync", "0007_registration_lifecycle_constraints")]
SCHEMA = [("sync", "0008_outbox_retry_state")]
AFTER = [("sync", "0009_outbox_retry_age_data")]


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


def _make_legacy_outbox(apps, alias, **overrides):
    Outbox = apps.get_model("sync", "OutboxMessage")
    now = datetime(2020, 1, 1, tzinfo=UTC)
    fields = {
        "event_id": uuid.uuid4(),
        "domain_event": None,
        "event_type": "activity.completed",
        "schema_version": 1,
        "source_node_id": uuid.uuid4(),
        "occurred_at": now,
        "aggregate_type": "attempt",
        "aggregate_public_id": uuid.uuid4(),
        "payload": {"score": 1, "max_score": 1},
        "state": "pending",
        "attempts": 0,
        "next_attempt_at": now,
        "claimed_at": None,
        "lease_until": None,
        "claim_token": None,
        "delivered_at": None,
        "rejection_code": "",
        "rejection_reason": "",
    }
    fields.update(overrides)
    row = Outbox.objects.using(alias).create(**fields)
    # created_at is auto_now_add -- override it directly to simulate a
    # genuinely old legacy row, distinct from next_attempt_at/due time.
    if "created_at" in overrides:
        Outbox.objects.using(alias).filter(pk=row.pk).update(created_at=overrides["created_at"])
        row.refresh_from_db()
    return row


def test_legacy_failure_age_uses_created_at_without_reset(scratch):
    apps = migrate(scratch, BEFORE)
    original_created_at = datetime(2019, 6, 1, tzinfo=UTC)
    original_due = datetime(2020, 3, 1, tzinfo=UTC)
    original_token = uuid.uuid4()
    original_lease_until = datetime(2020, 3, 1, 0, 5, tzinfo=UTC)
    event_id = uuid.uuid4()
    original_attempts = 7

    row = _make_legacy_outbox(
        apps,
        scratch,
        event_id=event_id,
        state="pending",
        attempts=original_attempts,
        next_attempt_at=original_due,
        claim_token=original_token,
        lease_until=original_lease_until,
        created_at=original_created_at,
    )

    # Capture the complete immutable envelope and every existing delivery
    # field before migrating, so the assertions below can prove exact
    # preservation, not merely spot-checks.
    Outbox = apps.get_model("sync", "OutboxMessage")
    before = Outbox.objects.using(scratch).values().get(pk=row.pk)

    migrate(scratch, SCHEMA)
    apps = migrate(scratch, AFTER)

    Outbox = apps.get_model("sync", "OutboxMessage")
    migrated = Outbox.objects.using(scratch).get(event_id=event_id)

    # Required core assertions (exact task wording).
    assert migrated.first_failure_at == original_created_at
    assert migrated.attempts == original_attempts
    assert migrated.next_attempt_at == original_due
    assert migrated.claim_token == original_token

    # Full envelope/delivery-field preservation.
    after = Outbox.objects.using(scratch).values().get(pk=row.pk)
    for field_name, original_value in before.items():
        if field_name in ("first_failure_at", "last_failure_category", "last_http_status"):
            continue
        assert after[field_name] == original_value, field_name
    assert after["last_failure_category"] == ""
    assert after["last_http_status"] is None


# --- all legacy state/attempt combinations ----------------------------------


@pytest.mark.parametrize(
    "state,attempts",
    [
        ("pending", 0),
        ("pending", 5),
        ("in_flight", 0),
        ("in_flight", 3),
        ("delivered", 0),
        ("delivered", 2),
        ("dead_letter", 0),
        ("dead_letter", 10),
    ],
)
def test_all_state_and_attempt_combinations_preserve_state_and_anchor_correctly(
    scratch, state, attempts
):
    apps = migrate(scratch, BEFORE)
    created_at = datetime(2021, 5, 1, tzinfo=UTC)
    row = _make_legacy_outbox(apps, scratch, state=state, attempts=attempts, created_at=created_at)

    migrate(scratch, SCHEMA)
    apps = migrate(scratch, AFTER)
    Outbox = apps.get_model("sync", "OutboxMessage")
    migrated = Outbox.objects.using(scratch).get(pk=row.pk)

    assert migrated.state == state
    assert migrated.attempts == attempts
    if attempts > 0:
        assert migrated.first_failure_at == created_at
    else:
        assert migrated.first_failure_at is None
    assert migrated.last_failure_category == ""
    assert migrated.last_http_status is None


def test_migration_uses_created_at_not_next_attempt_at_for_the_age_anchor(scratch):
    apps = migrate(scratch, BEFORE)
    old_created_at = datetime(2018, 1, 1, tzinfo=UTC)
    recent_due = datetime(2026, 9, 1, tzinfo=UTC)
    row = _make_legacy_outbox(
        apps, scratch, attempts=4, created_at=old_created_at, next_attempt_at=recent_due
    )

    migrate(scratch, SCHEMA)
    apps = migrate(scratch, AFTER)
    Outbox = apps.get_model("sync", "OutboxMessage")
    migrated = Outbox.objects.using(scratch).get(pk=row.pk)

    assert migrated.first_failure_at == old_created_at
    assert migrated.first_failure_at != recent_due
    assert migrated.next_attempt_at == recent_due


def test_migration_handles_a_recently_created_retrying_row_normally(scratch):
    apps = migrate(scratch, BEFORE)
    recent_created_at = datetime(2026, 9, 1, tzinfo=UTC)
    row = _make_legacy_outbox(
        apps,
        scratch,
        attempts=1,
        created_at=recent_created_at,
        next_attempt_at=recent_created_at + timedelta(minutes=5),
    )

    migrate(scratch, SCHEMA)
    apps = migrate(scratch, AFTER)
    Outbox = apps.get_model("sync", "OutboxMessage")
    migrated = Outbox.objects.using(scratch).get(pk=row.pk)

    assert migrated.first_failure_at == recent_created_at


# --- exhausted vs. nonexhausted fixtures: Task 7 must not decide eligibility


def test_exhausted_and_nonexhausted_legacy_fixtures_are_left_for_task_8(scratch):
    apps = migrate(scratch, BEFORE)
    very_old = datetime(2015, 1, 1, tzinfo=UTC)
    recent = datetime(2026, 9, 1, tzinfo=UTC)

    # Looks "potentially exhausted" to a future Task 8 policy: very old
    # first failure, very many attempts -- but Task 7 must not decide.
    exhausted_looking = _make_legacy_outbox(
        apps,
        scratch,
        state="pending",
        attempts=999,
        created_at=very_old,
        next_attempt_at=recent,
    )
    # Looks "potentially nonexhausted": a single recent attempt.
    nonexhausted_looking = _make_legacy_outbox(
        apps,
        scratch,
        state="pending",
        attempts=1,
        created_at=recent,
        next_attempt_at=recent,
    )

    migrate(scratch, SCHEMA)
    apps = migrate(scratch, AFTER)
    Outbox = apps.get_model("sync", "OutboxMessage")

    exhausted_after = Outbox.objects.using(scratch).get(pk=exhausted_looking.pk)
    nonexhausted_after = Outbox.objects.using(scratch).get(pk=nonexhausted_looking.pk)

    # Task 7 only anchors first_failure_at; it never dead-letters, never
    # changes state, and never computes eligibility -- that is Task 8.
    assert exhausted_after.state == "pending"
    assert exhausted_after.attempts == 999
    assert exhausted_after.first_failure_at == very_old
    assert nonexhausted_after.state == "pending"
    assert nonexhausted_after.attempts == 1
    assert nonexhausted_after.first_failure_at == recent


# --- idempotence -------------------------------------------------------------


def test_forward_data_algorithm_is_idempotent(scratch):
    import importlib
    from types import SimpleNamespace

    apps = migrate(scratch, BEFORE)
    created_at = datetime(2020, 6, 1, tzinfo=UTC)
    _make_legacy_outbox(apps, scratch, attempts=0, created_at=created_at)
    _make_legacy_outbox(apps, scratch, attempts=2, created_at=created_at)

    migrate(scratch, SCHEMA)
    apps = migrate(scratch, AFTER)
    Outbox = apps.get_model("sync", "OutboxMessage")
    first_pass = list(Outbox.objects.using(scratch).order_by("pk").values())

    # Re-run the migration's OWN forward function a second time, against
    # the same historical apps/connection -- never a reimplementation, and
    # never the current runtime model.
    retry_age_migration = importlib.import_module(
        "linuxlab.sync.migrations.0009_outbox_retry_age_data"
    )
    retry_age_migration.seed_first_failure_at(apps, SimpleNamespace(connection=connections[scratch]))

    second_pass = list(Outbox.objects.using(scratch).order_by("pk").values())
    assert second_pass == first_pass


# --- irreversibility ----------------------------------------------------------


def test_0009_reverse_is_irreversible_and_preserves_state(scratch):
    apps = migrate(scratch, BEFORE)
    old_created_at = datetime(2019, 1, 1, tzinfo=UTC)
    _make_legacy_outbox(apps, scratch, attempts=3, created_at=old_created_at)

    migrate(scratch, SCHEMA)
    apps = migrate(scratch, AFTER)
    Outbox = apps.get_model("sync", "OutboxMessage")
    before_reverse = list(Outbox.objects.using(scratch).order_by("pk").values())

    with pytest.raises(IrreversibleError):
        migrate(scratch, SCHEMA)

    recorder = MigrationRecorder(connections[scratch])
    assert AFTER[0] in recorder.applied_migrations()
    after_attempt = list(Outbox.objects.using(scratch).order_by("pk").values())
    assert after_attempt == before_reverse


def test_0008_structural_reverse_is_ordinary_and_distinct_from_0009_irreversibility(scratch):
    """0008 alone (never followed by 0009) is an ordinary, ordinary
    structural schema reversal -- distinct from 0009's semantic
    irreversibility, which exists because 0009's data cannot be
    distinguished from genuine post-migration evidence, not because
    reversing a RunPython is mechanically hard."""
    apps = migrate(scratch, BEFORE)
    row = _make_legacy_outbox(apps, scratch)

    apps = migrate(scratch, SCHEMA)
    Outbox = apps.get_model("sync", "OutboxMessage")
    assert Outbox._meta.get_field("first_failure_at")

    apps = migrate(scratch, BEFORE)
    Outbox = apps.get_model("sync", "OutboxMessage")
    field_names = {field.name for field in Outbox._meta.get_fields()}
    assert "first_failure_at" not in field_names
    assert "last_failure_category" not in field_names
    assert "last_http_status" not in field_names

    recorder = MigrationRecorder(connections[scratch])
    assert SCHEMA[0] not in recorder.applied_migrations()
    assert Outbox.objects.using(scratch).filter(pk=row.pk).exists()


# --- constraint enforcement ---------------------------------------------------


def test_postgresql_enforces_retry_state_constraints_after_migration(scratch):
    apps = migrate(scratch, AFTER)
    Outbox = apps.get_model("sync", "OutboxMessage")

    def _make(**overrides):
        return _make_legacy_outbox(apps, scratch, event_id=uuid.uuid4(), **overrides)

    _make(last_failure_category="")
    for category in (
        "PERMANENT_REJECTION",
        "AUTH_OR_REGISTRATION_FAILURE",
        "TRANSIENT_FAILURE",
        "RATE_LIMITED",
    ):
        _make(last_failure_category=category)
    _make(last_http_status=None)
    _make(last_http_status=100)
    _make(last_http_status=599)

    for bad_category in ("SUCCESS", "success", "bogus", "permanent_rejection"):
        with pytest.raises(IntegrityError), transaction.atomic(using=scratch):
            _make(last_failure_category=bad_category)
    for bad_status in (0, 1, 99, 600, 1000, -1):
        with pytest.raises(IntegrityError), transaction.atomic(using=scratch):
            _make(last_http_status=bad_status)

    with connections[scratch].cursor() as cursor:
        constraints = connections[scratch].introspection.get_constraints(
            cursor, Outbox._meta.db_table
        )
    assert constraints["outbox_message_last_failure_category_allowed_values"]["check"]
    assert constraints["outbox_message_last_http_status_range"]["check"]
    assert constraints["outbox_message_state_allowed_values"]["check"]

    # No new state vocabulary and no new table under a plausible name.
    assert set(
        Outbox.objects.using(scratch).values_list("state", flat=True)
    ) <= {"pending", "in_flight", "delivered", "dead_letter"}
    with pytest.raises(LookupError):
        apps.get_model("sync", "OutboxRetryState")


def test_outbox_message_model_metadata_matches_migration_state(scratch):
    apps = migrate(scratch, AFTER)
    Outbox = apps.get_model("sync", "OutboxMessage")

    first_failure_at = Outbox._meta.get_field("first_failure_at")
    assert first_failure_at.null and first_failure_at.blank

    last_failure_category = Outbox._meta.get_field("last_failure_category")
    assert last_failure_category.max_length == 32
    assert last_failure_category.blank
    assert last_failure_category.default == ""

    last_http_status = Outbox._meta.get_field("last_http_status")
    assert last_http_status.null and last_http_status.blank

    state_field = Outbox._meta.get_field("state")
    assert {value for value, _label in state_field.choices} == {
        "pending",
        "in_flight",
        "delivered",
        "dead_letter",
    }


# --- Corrective B Task 8 §22: claim_outbox_batch() eligibility against a
# genuinely migrated legacy row -----------------------------------------------
# claim_outbox_batch() always queries Django's `default` DB alias, so its
# preflight budget-eligibility behavior against a migrated legacy row can
# only be proven from a real, separate Django process pointed at the
# scratch database via POSTGRES_DB -- never a second in-process alias
# (claim_outbox_batch() has no alias parameter to accept one). Everything
# up to and including the 0008/0009 migration itself still runs in-process
# against the scratch alias, exactly like every other test in this file;
# only the final claim_outbox_batch() call needs the subprocess.

_ELIGIBILITY_PROBE_SCRIPT = """
import json
import sys
import django

django.setup()

from linuxlab.sync.models import OutboxMessage
from linuxlab.sync.outbox import claim_outbox_batch

exhausted_id, eligible_id = sys.argv[1], sys.argv[2]

claimed = claim_outbox_batch(max_events=10, max_bytes=1_000_000, lease_seconds=60)
claimed_ids = sorted(str(row.event_id) for row in claimed)

exhausted_after = OutboxMessage.objects.get(event_id=exhausted_id)
eligible_after = OutboxMessage.objects.get(event_id=eligible_id)

print(json.dumps({
    "claimed_ids": claimed_ids,
    "exhausted_state": exhausted_after.state,
    "exhausted_attempts": exhausted_after.attempts,
    "exhausted_last_failure_category": exhausted_after.last_failure_category,
    "eligible_state": eligible_after.state,
}))
"""


def test_claim_outbox_batch_eligibility_against_a_genuinely_migrated_legacy_row(scratch):
    apps = migrate(scratch, BEFORE)
    now = datetime.now(UTC)
    very_old = datetime(2015, 1, 1, tzinfo=UTC)
    # Genuinely recent relative to REAL wall-clock time (the retry-age
    # budget is measured against actual now(), not a fixed calendar
    # date) -- comfortably inside the default 24h max_retry_age.
    recent = now - timedelta(minutes=5)

    # A legacy row whose attempts already meet/exceed the default
    # max_attempts(10) budget -- created and migrated exactly like the
    # "exhausted_looking" fixture in
    # test_exhausted_and_nonexhausted_legacy_fixtures_are_left_for_task_8,
    # except this test actually exercises Task 8's claim-time preflight
    # eligibility decision against it (Task 7 deliberately did not).
    exhausted = _make_legacy_outbox(
        apps,
        scratch,
        state="pending",
        attempts=10,
        created_at=very_old,
        next_attempt_at=now,
    )
    eligible = _make_legacy_outbox(
        apps,
        scratch,
        state="pending",
        attempts=1,
        created_at=recent,
        next_attempt_at=now,
    )

    migrate(scratch, SCHEMA)
    migrate(scratch, AFTER)

    db_settings = connections[scratch].settings_dict
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "linuxlab.settings.node",
        "POSTGRES_DB": db_settings["NAME"],
        "POSTGRES_USER": str(db_settings["USER"]),
        "POSTGRES_PASSWORD": str(db_settings["PASSWORD"]),
        "POSTGRES_HOST": str(db_settings["HOST"]),
        "POSTGRES_PORT": str(db_settings["PORT"]),
    }

    result = subprocess.run(
        [sys.executable, "-c", _ELIGIBILITY_PROBE_SCRIPT, str(exhausted.event_id), str(eligible.event_id)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    probe = json.loads(result.stdout.strip().splitlines()[-1])

    # The exhausted migrated row is never claimed and never sent -- it is
    # terminalized as a preflight eligibility decision, so its attempts
    # count and failure evidence are left exactly as the migration left
    # them (this is NOT a failed send).
    assert probe["exhausted_state"] == "dead_letter"
    assert probe["exhausted_attempts"] == 10
    assert probe["exhausted_last_failure_category"] == ""
    assert str(exhausted.event_id) not in probe["claimed_ids"]

    # The budget-eligible migrated row claims normally.
    assert probe["claimed_ids"] == [str(eligible.event_id)]
    assert probe["eligible_state"] == "in_flight"
