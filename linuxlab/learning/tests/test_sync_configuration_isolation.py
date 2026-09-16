"""Local learning commits even when optional sync admission is misconfigured."""

import io
import json
import os
import subprocess
import sys
import uuid
from unittest.mock import patch

import pytest
from django.conf import settings as django_settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import Client, override_settings

from linuxlab.core.models import DomainEvent
from linuxlab.learning.models import Attempt, Progress, Result
from linuxlab.learning.services import claim_activity_completion, start_attempt, submit_attempt
from linuxlab.sync.configuration import (
    SyncConfigurationError,
    load_sync_configuration,
    parse_sync_setting,
)
from linuxlab.sync.enqueue import enqueue_sync_event
from linuxlab.sync.models import OutboxMessage
from linuxlab.sync.tests.test_task14_enqueue import (
    _make_learner,
    _make_published_activity_version,
)

ENABLED = "LINUXLAB_SYNC_ENABLED"
CLAIM_URL = "/api/v1/learning/claim-progress/"
WARNING = "SYNC_ENQUEUE_SKIPPED_INVALID_ENABLED: optional sync export skipped."
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_NODE_ID = str(uuid.uuid4())
    version, registry = _make_published_activity_version()
    # A real unrelated pending export must survive both local operations.
    with override_settings(LINUXLAB_SYNC_ENABLED=True):
        claim_activity_completion(_make_learner(), version)
    return _make_learner(), version, registry


def claim(client, version):
    return client.post(
        CLAIM_URL,
        data=json.dumps(
            {
                "claims": [
                    {
                        "activity_public_id": str(version.definition.public_id),
                        "activity_version": version.version,
                    }
                ]
            }
        ),
        content_type="application/json",
    )


def assert_local_result(learner, version, *, claimed):
    attempt = Attempt.objects.get(learner=learner, activity_version=version)
    assert attempt.submitted_at is not None
    assert attempt.submission == (
        {"source": "guest_claim"} if claimed else {"answer": "ANSWER_SENTINEL"}
    )
    result = Result.objects.get(attempt=attempt)
    assert (result.score, result.max_score) == (1, 1)
    if claimed:
        assert result.details["source"] == "guest_claim"
    progress = Progress.objects.get(learner=learner, activity_definition=version.definition)
    assert progress.completed and progress.best_score == 1
    event = DomainEvent.objects.get(event_type="activity.completed", aggregate_id=attempt.public_id)
    assert event.payload["attempt_public_id"] == str(attempt.public_id)
    assert event.payload["learner_public_id"] == str(learner.public_id)
    assert event.payload["score"] == event.payload["max_score"] == 1
    return event


def assert_strict_failure(invalid):
    before = list(OutboxMessage.objects.order_by("pk").values())
    assert django_settings.LINUXLAB_SYNC_ENABLED is invalid
    with pytest.raises(SyncConfigurationError) as error:
        load_sync_configuration(purpose="worker")
    assert error.value.setting_name == ENABLED
    output = io.StringIO()
    with (
        patch("linuxlab.sync.management.commands.sync_events.HttpSyncTransport") as transport,
        patch("linuxlab.sync.management.commands.sync_events.flush_outbox") as flush,
        pytest.raises(CommandError) as command_error,
    ):
        call_command("sync_events", "--once", stdout=output, stderr=output)
    transport.assert_not_called()
    flush.assert_not_called()
    assert "SENTINEL" not in output.getvalue() + str(command_error.value)
    with pytest.raises(SyncConfigurationError):
        bool(invalid)
    assert list(OutboxMessage.objects.order_by("pk").values()) == before


@pytest.mark.parametrize("operation", ["claim", "submit"])
def test_malformed_enabled_preserves_local_learning(world, settings, caplog, operation):
    learner, version, registry = world
    invalid = parse_sync_setting(ENABLED, "CONFIG_SECRET_SENTINEL")
    settings.LINUXLAB_SYNC_ENABLED = invalid
    before = list(OutboxMessage.objects.order_by("pk").values())
    client = Client()
    client.force_login(learner.account)
    with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no HTTP")):
        if operation == "claim":
            response = claim(client, version)
            assert response.status_code == 200, response.json()
            assert response.json() == {"learner_public_id": str(learner.public_id), "claimed": 1}
            assert "SENTINEL" not in response.content.decode()
            assert "LINUXLAB_" not in response.content.decode()
        else:
            attempt = start_attempt(learner, version)
            submit_attempt(attempt, {"answer": "ANSWER_SENTINEL"}, registry)
    assert_local_result(learner, version, claimed=operation == "claim")
    assert list(OutboxMessage.objects.order_by("pk").values()) == before
    assert WARNING in caplog.messages
    assert "SENTINEL" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert settings.LINUXLAB_SYNC_ENABLED is invalid
    with pytest.raises(SyncConfigurationError):
        bool(invalid)
    assert_strict_failure(invalid)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("operation", ["claim", "submit"])
def test_valid_admission_preserves_identity_and_idempotency(
    world, settings, caplog, enabled, operation
):
    learner, version, registry = world
    settings.LINUXLAB_SYNC_ENABLED = enabled
    if operation == "claim":
        client = Client()
        client.force_login(learner.account)
        assert claim(client, version).json()["claimed"] == 1
        assert claim(client, version).json()["claimed"] == 0
    else:
        attempt = start_attempt(learner, version)
        submit_attempt(attempt, {"answer": "ANSWER_SENTINEL"}, registry)
        with pytest.raises(ValueError, match="already been submitted"):
            submit_attempt(attempt, {}, registry)
    event = assert_local_result(learner, version, claimed=operation == "claim")
    assert OutboxMessage.objects.count() == (2 if enabled else 1)
    if enabled:
        row = OutboxMessage.objects.get(domain_event=event)
        assert row.event_id == event.event_id
        assert row.aggregate_public_id == event.aggregate_id
        assert row.source_node_id == uuid.UUID(settings.LINUXLAB_NODE_ID)
        assert "source" not in row.payload and "answer" not in row.payload
    assert WARNING not in caplog.messages


def test_invalid_claim_still_rejected_without_local_changes(world, settings):
    learner, version, _registry = world
    settings.LINUXLAB_SYNC_ENABLED = parse_sync_setting(ENABLED, "CONFIG_SECRET_SENTINEL")
    client = Client()
    client.force_login(learner.account)
    before = {
        model: list(model.objects.order_by("pk").values())
        for model in (Attempt, Result, Progress, DomainEvent, OutboxMessage)
    }
    response = client.post(
        CLAIM_URL,
        data=json.dumps(
            {
                "claims": [
                    {
                        "activity_public_id": str(version.definition.public_id),
                        "activity_version": version.version,
                    },
                    {"activity_public_id": str(uuid.uuid4()), "activity_version": 1},
                ]
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 400
    for model, rows in before.items():
        assert list(model.objects.order_by("pk").values()) == rows


def test_real_duplicate_outbox_failure_rolls_back_local_action(world, settings):
    learner, version, _registry = world
    settings.LINUXLAB_SYNC_ENABLED = True
    before = {
        model: list(model.objects.order_by("pk").values())
        for model in (Attempt, Result, Progress, DomainEvent, OutboxMessage)
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        claim_activity_completion(learner, version)
        event = DomainEvent.objects.get(aggregate_id=Attempt.objects.get(learner=learner).public_id)
        enqueue_sync_event(event, node_id=settings.LINUXLAB_NODE_ID, sync_enabled=True)
    for model, rows in before.items():
        assert list(model.objects.order_by("pk").values()) == rows


@pytest.mark.parametrize("raw", [False, "CONFIG_SECRET_SENTINEL"])
def test_anonymous_local_only_submission_stays_local(world, settings, raw):
    from linuxlab.identity.models import Learner

    _learner, version, registry = world
    anonymous = Learner.objects.create(account=None)
    settings.LINUXLAB_SYNC_ENABLED = parse_sync_setting(ENABLED, raw)
    before = list(OutboxMessage.objects.order_by("pk").values())
    submit_attempt(start_attempt(anonymous, version), {"answer": "ANSWER_SENTINEL"}, registry)
    assert_local_result(anonymous, version, claimed=False)
    assert list(OutboxMessage.objects.order_by("pk").values()) == before


def test_unregistered_event_stays_local(world, settings):
    from linuxlab.core.events import publish_event

    event = publish_event("local.only", {}, uuid.uuid4())
    before = list(OutboxMessage.objects.order_by("pk").values())
    assert enqueue_sync_event(event, node_id=settings.LINUXLAB_NODE_ID, sync_enabled=True) is None
    assert list(OutboxMessage.objects.order_by("pk").values()) == before


def test_fresh_node_malformed_environment_preserves_real_claim(world):
    learner, version, _registry = world
    before = list(OutboxMessage.objects.order_by("pk").values())
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
        DJANGO_SETTINGS_MODULE="linuxlab.settings.node",
        DJANGO_ALLOWED_HOSTS="testserver,localhost",
        LINUXLAB_SYNC_ENABLED="CONFIG_SECRET_SENTINEL",
    )
    script = """
import sys
from unittest.mock import patch
import django
django.setup()
from django.conf import settings
from django.test import Client
from linuxlab.identity.models import Learner
from linuxlab.activities.models import ActivityVersion
from linuxlab.learning.tests.test_sync_configuration_isolation import claim, assert_local_result, assert_strict_failure
from linuxlab.sync.models import OutboxMessage
learner = Learner.objects.get(pk=sys.argv[1])
version = ActivityVersion.objects.get(pk=sys.argv[2])
invalid = settings.LINUXLAB_SYNC_ENABLED
before = list(OutboxMessage.objects.order_by("pk").values())
with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no HTTP")):
    client = Client()
    client.force_login(learner.account)
    response = claim(client, version)
    assert response.status_code == 200, response.content
    assert response.json() == {"learner_public_id": str(learner.public_id), "claimed": 1}
    assert b"SENTINEL" not in response.content and b"LINUXLAB_" not in response.content
    assert_local_result(learner, version, claimed=True)
    assert list(OutboxMessage.objects.order_by("pk").values()) == before
    assert_strict_failure(invalid)
    for path in ("/health/", "/healthz", "/ready/", "/readyz"):
        response = client.get(path)
        assert response.status_code == 200 and response.json() == {"status": "ok"}
print("local committed; export skipped; worker rejected; readiness ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(learner.pk), str(version.pk)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "local committed; export skipped; worker rejected; readiness ok" in result.stdout
    assert WARNING in result.stderr
    assert "SENTINEL" not in result.stdout + result.stderr
    assert_local_result(learner, version, claimed=True)
    assert list(OutboxMessage.objects.order_by("pk").values()) == before
