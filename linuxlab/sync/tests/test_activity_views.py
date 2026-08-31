"""T15-D27 S11, refined by the T15-D27 corrective: an explicit Node
Start -> Attempt -> Submit HTTP flow. Landing GET never creates an
Attempt; POST start pins one against the current active package and
redirects to its own page; GET/POST on that page resolve entirely from
the Attempt's own pinned installed artifact, never re-resolving "the
current active package"; submit rejects any session/identity that does
not match the Attempt's own learner."""

import json
import uuid
from unittest.mock import patch

import pytest
from django.contrib.sessions.models import Session
from django.test import Client, override_settings

from linuxlab.core.models import DomainEvent
from linuxlab.identity.models import Learner
from linuxlab.identity.services import (
    resolve_existing_guest_learner,
    resolve_or_create_guest_learner,
)
from linuxlab.learning.models import Attempt, Progress, Result
from linuxlab.publication.package import build_package
from linuxlab.sync.models import OutboxMessage
from linuxlab.sync.services import activate_package, import_package

from .conftest import republish

pytestmark = pytest.mark.django_db(transaction=True)

_NODE_SETTINGS = {
    "LINUXLAB_CAN_IMPORT_PACKAGES": True,
    "LINUXLAB_CAN_SERVE_LEARNER_CONTENT": True,
}


def _stage(world, private_key, public_key):
    package_bytes = build_package(world["snapshot"], world["activity_registry"], private_key)
    return import_package(package_bytes, world["activity_registry"], public_key)


def _landing_url(world):
    return (
        f"/learn/{world['snapshot'].source_public_id}/activity/"
        f"{world['definition'].public_id}/{world['activity_version'].version}/"
    )


def _start_url(world):
    return _landing_url(world) + "start/"


def _attempt_url(attempt):
    return f"/learn/attempt/{attempt.public_id}/"


def _attempt_submit_url(attempt):
    return _attempt_url(attempt) + "submit/"


def _correct_submission(world):
    return {
        "responses": [
            {
                "question_public_id": str(world["question"].public_id),
                "question_version": world["question_version"].version,
                "selected_option_id": "a",
            }
        ]
    }


def _start_attempt(client, world):
    """POST start and return the created Attempt, reading its public_id
    back off the redirect Location header (never assumed/synthesized)."""
    response = client.post(_start_url(world))
    assert response.status_code == 303
    attempt_public_id = response["Location"].rstrip("/").rsplit("/", 1)[-1]
    return Attempt.objects.get(public_id=attempt_public_id)


# --- landing GET -------------------------------------------------------------


@override_settings(**_NODE_SETTINGS)
def test_landing_get_renders_learner_safe_content_and_creates_no_attempt(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().get(_landing_url(quiz_world))

    assert response.status_code == 200
    rendered = response.content.decode()
    assert "Which command lists files?" in rendered
    assert "answer_key" not in rendered
    assert "correct_option_id" not in rendered
    assert "scoring" not in rendered
    assert Attempt.objects.count() == 0


@override_settings(**_NODE_SETTINGS)
def test_landing_get_returns_503_without_an_active_package():
    response = Client().get(f"/learn/{uuid.uuid4()}/activity/{uuid.uuid4()}/1/")

    assert response.status_code == 503


@override_settings(**_NODE_SETTINGS)
def test_landing_get_returns_404_for_unknown_activity(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().get(f"/learn/{quiz_world['snapshot'].source_public_id}/activity/{uuid.uuid4()}/1/")

    assert response.status_code == 404


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True, LINUXLAB_CAN_SERVE_LEARNER_CONTENT=False)
def test_landing_get_rejected_under_central_profile(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().get(_landing_url(quiz_world))

    assert response.status_code == 403


# --- POST start ---------------------------------------------------------------


@override_settings(**_NODE_SETTINGS)
def test_post_start_pins_an_attempt_and_redirects_to_its_own_page(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    attempt = _start_attempt(Client(), quiz_world)

    assert attempt.installed_activity_version.activity_public_id == quiz_world["definition"].public_id
    assert attempt.submitted_at is None


@override_settings(**_NODE_SETTINGS)
def test_post_start_delegates_learner_resolution_to_identity_service(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    with patch(
        "linuxlab.sync.views.resolve_or_create_guest_learner", wraps=resolve_or_create_guest_learner
    ) as spy:
        response = Client().post(_start_url(quiz_world))

    assert response.status_code == 303
    spy.assert_called_once()


@override_settings(**_NODE_SETTINGS)
def test_post_start_returns_503_without_an_active_package():
    response = Client().post(f"/learn/{uuid.uuid4()}/activity/{uuid.uuid4()}/1/start/")

    assert response.status_code == 503


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True, LINUXLAB_CAN_SERVE_LEARNER_CONTENT=False)
def test_post_start_rejected_under_central_profile(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().post(_start_url(quiz_world))

    assert response.status_code == 403


@override_settings(**_NODE_SETTINGS)
def test_post_start_always_targets_the_current_active_package(quiz_world, keypair):
    private_key, public_key = keypair
    v1 = _stage(quiz_world, private_key, public_key)
    activate_package(v1)

    v2_snapshot = republish(quiz_world)
    v2_bytes = build_package(v2_snapshot, quiz_world["activity_registry"], private_key)
    v2 = import_package(v2_bytes, quiz_world["activity_registry"], public_key)
    activate_package(v2)

    attempt = _start_attempt(Client(), quiz_world)

    assert attempt.installed_activity_version.package_id == v2.pk


# --- Attempt GET ---------------------------------------------------------------


@override_settings(**_NODE_SETTINGS)
def test_attempt_get_renders_learner_safe_payload(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()
    attempt = _start_attempt(client, quiz_world)

    response = client.get(_attempt_url(attempt))

    assert response.status_code == 200
    rendered = response.content.decode()
    assert "Which command lists files?" in rendered
    assert "answer_key" not in rendered
    assert "correct_option_id" not in rendered
    assert "scoring" not in rendered


@override_settings(**_NODE_SETTINGS)
def test_attempt_get_returns_404_for_unknown_attempt():
    response = Client().get(f"/learn/attempt/{uuid.uuid4()}/")

    assert response.status_code == 404


@override_settings(**_NODE_SETTINGS)
def test_attempt_get_ignores_a_newly_activated_package_and_never_re_resolves_active(
    quiz_world, keypair
):
    """T15-D27 corrective: GET landing A -> POST start A -> Attempt X
    pinned to A -> activate B -> GET Attempt X still renders A (never the
    current active package B)."""
    private_key, public_key = keypair
    v1 = _stage(quiz_world, private_key, public_key)
    activate_package(v1)
    client = Client()

    landing = client.get(_landing_url(quiz_world))
    assert landing.status_code == 200

    attempt = _start_attempt(client, quiz_world)
    assert attempt.installed_activity_version.package_id == v1.pk

    v2_snapshot = republish(quiz_world)
    v2_bytes = build_package(v2_snapshot, quiz_world["activity_registry"], private_key)
    v2 = import_package(v2_bytes, quiz_world["activity_registry"], public_key)
    activate_package(v2)

    response = client.get(_attempt_url(attempt))

    assert response.status_code == 200
    attempt.refresh_from_db()
    assert attempt.installed_activity_version.package_id == v1.pk
    assert attempt.installed_activity_version.package_id != v2.pk


# --- Attempt POST submit --------------------------------------------------------


@override_settings(**_NODE_SETTINGS)
def test_attempt_submit_grades_the_pinned_attempt_and_returns_score(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()
    attempt = _start_attempt(client, quiz_world)

    response = client.post(
        _attempt_submit_url(attempt), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["score"] == 1
    assert body["max_score"] == 1
    assert body["attempt_public_id"] == str(attempt.public_id)


@override_settings(**_NODE_SETTINGS)
def test_attempt_submit_persists_result_and_progress_without_creating_a_new_attempt(
    quiz_world, keypair
):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()
    attempt = _start_attempt(client, quiz_world)
    assert Attempt.objects.count() == 1

    client.post(
        _attempt_submit_url(attempt), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    assert Attempt.objects.count() == 1
    attempt.refresh_from_db()
    assert hasattr(attempt, "result")
    progress = Progress.objects.get(
        learner=attempt.learner, activity_definition_public_id=quiz_world["definition"].public_id
    )
    assert progress.completed is True


@override_settings(**_NODE_SETTINGS)
def test_attempt_submit_emits_activity_completed_event(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()
    attempt = _start_attempt(client, quiz_world)

    client.post(
        _attempt_submit_url(attempt), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    assert DomainEvent.objects.filter(
        event_type="activity.completed", aggregate_id=attempt.public_id
    ).exists()


@override_settings(**_NODE_SETTINGS)
def test_attempt_submit_response_has_no_grading_material(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()
    attempt = _start_attempt(client, quiz_world)

    response = client.post(
        _attempt_submit_url(attempt), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    rendered = response.content.decode()
    assert "answer_key" not in rendered
    assert "correct_option_id" not in rendered


@override_settings(**_NODE_SETTINGS)
def test_attempt_submit_rejects_invalid_json_body(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()
    attempt = _start_attempt(client, quiz_world)

    response = client.post(_attempt_submit_url(attempt), data="not json", content_type="application/json")

    assert response.status_code == 400


@override_settings(**_NODE_SETTINGS)
def test_attempt_submit_returns_404_for_unknown_attempt():
    response = Client().post(
        f"/learn/attempt/{uuid.uuid4()}/submit/", data="{}", content_type="application/json"
    )

    assert response.status_code == 404


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True, LINUXLAB_CAN_SERVE_LEARNER_CONTENT=False)
def test_attempt_submit_rejected_under_central_profile(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()

    with override_settings(**_NODE_SETTINGS):
        attempt = _start_attempt(client, quiz_world)

    response = client.post(_attempt_submit_url(attempt), data="{}", content_type="application/json")

    assert response.status_code == 403


@override_settings(**_NODE_SETTINGS)
def test_attempt_submit_does_not_invalidate_when_current_package_becomes_inactive(quiz_world, keypair):
    """T15-D27 corrective: an already-started Attempt must still grade
    successfully even after a newer package has since become active for
    this source (the pinned package itself is no longer "the current
    active package", but that must not invalidate submission)."""
    private_key, public_key = keypair
    v1 = _stage(quiz_world, private_key, public_key)
    activate_package(v1)
    client = Client()
    attempt = _start_attempt(client, quiz_world)

    v2_snapshot = republish(quiz_world)
    v2_bytes = build_package(v2_snapshot, quiz_world["activity_registry"], private_key)
    v2 = import_package(v2_bytes, quiz_world["activity_registry"], public_key)
    activate_package(v2)

    response = client.post(
        _attempt_submit_url(attempt), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    assert response.status_code == 200
    assert response.json()["score"] == 1


# --- security/identity boundary (T15-D27 corrective) --------------------------


@override_settings(**_NODE_SETTINGS)
def test_a_different_session_cannot_submit_someone_elses_attempt(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    owner_client = Client()
    attempt = _start_attempt(owner_client, quiz_world)

    other_client = Client()  # a fresh, unrelated session/cookie jar
    response = other_client.post(
        _attempt_submit_url(attempt), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    assert response.status_code == 403
    attempt.refresh_from_db()
    assert attempt.submitted_at is None


@override_settings(**_NODE_SETTINGS)
def test_a_posted_learner_public_id_in_the_submit_body_is_ignored(quiz_world, keypair):
    """An arbitrary/forged learner identity in the POST body itself must
    have zero effect: only the session/auth-resolved identity is ever
    trusted for the ownership check."""
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    owner_client = Client()
    attempt = _start_attempt(owner_client, quiz_world)

    other_client = Client()
    forged_submission = dict(_correct_submission(quiz_world))
    forged_submission["learner_public_id"] = str(attempt.learner.public_id)

    response = other_client.post(
        _attempt_submit_url(attempt), data=json.dumps(forged_submission), content_type="application/json"
    )

    assert response.status_code == 403
    attempt.refresh_from_db()
    assert attempt.submitted_at is None


@override_settings(**_NODE_SETTINGS)
def test_same_session_can_start_then_submit_its_own_attempt(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()

    attempt = _start_attempt(client, quiz_world)
    response = client.post(
        _attempt_submit_url(attempt), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    assert response.status_code == 200


# --- T15-D27 final corrective: read-only authorization, zero mutation on deny


def _mutation_snapshot():
    return {
        "Learner": Learner.objects.count(),
        "Session": Session.objects.count(),
        "Attempt": Attempt.objects.count(),
        "Result": Result.objects.count(),
        "Progress": Progress.objects.count(),
        "DomainEvent": DomainEvent.objects.count(),
        "OutboxMessage": OutboxMessage.objects.count(),
    }


@override_settings(**_NODE_SETTINGS)
def test_wrong_session_get_denies_and_mutates_nothing(quiz_world, keypair):
    """GET on an existing Attempt must enforce ownership (it previously
    did not) and a denied GET must leave zero trace: no Learner created,
    no session row created, no business/event/outbox state touched."""
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    owner_client = Client()
    attempt = _start_attempt(owner_client, quiz_world)

    other_client = Client()
    before = _mutation_snapshot()

    response = other_client.get(_attempt_url(attempt))

    after = _mutation_snapshot()
    assert response.status_code in (403, 404)
    assert after == before
    attempt.refresh_from_db()
    assert attempt.submitted_at is None


@override_settings(**_NODE_SETTINGS)
def test_wrong_session_submit_denies_and_mutates_nothing(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    owner_client = Client()
    attempt = _start_attempt(owner_client, quiz_world)

    other_client = Client()
    before = _mutation_snapshot()

    response = other_client.post(
        _attempt_submit_url(attempt), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    after = _mutation_snapshot()
    assert response.status_code == 403
    assert after == before
    attempt.refresh_from_db()
    assert attempt.submitted_at is None


@override_settings(**_NODE_SETTINGS)
def test_owner_can_still_get_their_own_attempt(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()
    attempt = _start_attempt(client, quiz_world)

    response = client.get(_attempt_url(attempt))

    assert response.status_code == 200
    rendered = response.content.decode()
    assert "answer_key" not in rendered


@override_settings(**_NODE_SETTINGS)
def test_attempt_get_and_submit_delegate_to_the_read_only_identity_service(quiz_world, keypair):
    """The existing-Attempt authorization path must resolve identity
    through resolve_existing_guest_learner() -- never
    resolve_or_create_guest_learner() (the create-capable service, which
    remains reserved for POST /start/ only)."""
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()
    attempt = _start_attempt(client, quiz_world)

    with patch(
        "linuxlab.sync.views.resolve_existing_guest_learner", wraps=resolve_existing_guest_learner
    ) as spy:
        get_response = client.get(_attempt_url(attempt))
    assert get_response.status_code == 200
    spy.assert_called_once()

    with patch(
        "linuxlab.sync.views.resolve_existing_guest_learner", wraps=resolve_existing_guest_learner
    ) as spy:
        submit_response = client.post(
            _attempt_submit_url(attempt),
            data=json.dumps(_correct_submission(quiz_world)),
            content_type="application/json",
        )
    assert submit_response.status_code == 200
    spy.assert_called_once()
