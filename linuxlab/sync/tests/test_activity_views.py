"""T15-D27 S11: Node HTTP learner activity/submit surface -- a thin
adapter that starts+grades a fresh installed Attempt against the exact
pinned InstalledActivityVersion, never re-resolving the active package
mid-request, and never exposing grading material."""

import json
import uuid

import pytest
from django.test import Client, override_settings

from linuxlab.core.models import DomainEvent
from linuxlab.learning.models import Attempt, Progress
from linuxlab.publication.package import build_package
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


def _activity_url(world):
    return (
        f"/learn/{world['snapshot'].source_public_id}/activity/"
        f"{world['definition'].public_id}/{world['activity_version'].version}/"
    )


def _submit_url(world):
    return _activity_url(world) + "submit/"


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


# --- GET activity route -----------------------------------------------------


@override_settings(**_NODE_SETTINGS)
def test_get_activity_route_renders_learner_safe_payload(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().get(_activity_url(quiz_world))

    assert response.status_code == 200
    rendered = response.content.decode()
    assert "Which command lists files?" in rendered
    assert "answer_key" not in rendered
    assert "correct_option_id" not in rendered
    assert "scoring" not in rendered


@override_settings(**_NODE_SETTINGS)
def test_get_activity_route_returns_503_without_an_active_package():
    response = Client().get(f"/learn/{uuid.uuid4()}/activity/{uuid.uuid4()}/1/")

    assert response.status_code == 503


@override_settings(**_NODE_SETTINGS)
def test_get_activity_route_returns_404_for_unknown_activity(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().get(f"/learn/{quiz_world['snapshot'].source_public_id}/activity/{uuid.uuid4()}/1/")

    assert response.status_code == 404


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True, LINUXLAB_CAN_SERVE_LEARNER_CONTENT=False)
def test_get_activity_route_rejected_under_central_profile(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().get(_activity_url(quiz_world))

    assert response.status_code == 403


# --- POST submit route -------------------------------------------------------


@override_settings(**_NODE_SETTINGS)
def test_post_submit_grades_correct_response_and_returns_score(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().post(
        _submit_url(quiz_world), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["score"] == 1
    assert body["max_score"] == 1


@override_settings(**_NODE_SETTINGS)
def test_post_submit_persists_attempt_result_and_progress(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().post(
        _submit_url(quiz_world), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )
    attempt_public_id = response.json()["attempt_public_id"]

    attempt = Attempt.objects.get(public_id=attempt_public_id)
    assert attempt.installed_activity_version is not None
    assert attempt.activity_version_id is None
    assert hasattr(attempt, "result")

    progress = Progress.objects.get(
        learner=attempt.learner, activity_definition_public_id=quiz_world["definition"].public_id
    )
    assert progress.completed is True


@override_settings(**_NODE_SETTINGS)
def test_post_submit_emits_activity_completed_event(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().post(
        _submit_url(quiz_world), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )
    attempt_public_id = response.json()["attempt_public_id"]

    assert DomainEvent.objects.filter(
        event_type="activity.completed", aggregate_id=attempt_public_id
    ).exists()


@override_settings(**_NODE_SETTINGS)
def test_post_submit_response_has_no_grading_material(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().post(
        _submit_url(quiz_world), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    rendered = response.content.decode()
    assert "answer_key" not in rendered
    assert "correct_option_id" not in rendered


@override_settings(**_NODE_SETTINGS)
def test_post_submit_rejects_invalid_json_body(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().post(_submit_url(quiz_world), data="not json", content_type="application/json")

    assert response.status_code == 400


@override_settings(**_NODE_SETTINGS)
def test_post_submit_returns_503_without_an_active_package():
    response = Client().post(
        f"/learn/{uuid.uuid4()}/activity/{uuid.uuid4()}/1/submit/", data="{}", content_type="application/json"
    )

    assert response.status_code == 503


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True, LINUXLAB_CAN_SERVE_LEARNER_CONTENT=False)
def test_post_submit_rejected_under_central_profile(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))

    response = Client().post(_submit_url(quiz_world), data="{}", content_type="application/json")

    assert response.status_code == 403


@override_settings(**_NODE_SETTINGS)
def test_same_anonymous_session_reuses_one_learner_across_requests(quiz_world, keypair):
    private_key, public_key = keypair
    activate_package(_stage(quiz_world, private_key, public_key))
    client = Client()

    first = client.post(
        _submit_url(quiz_world), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    second_snapshot_response = client.post(
        _submit_url(quiz_world), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    first_attempt = Attempt.objects.get(public_id=first.json()["attempt_public_id"])
    second_attempt = Attempt.objects.get(public_id=second_snapshot_response.json()["attempt_public_id"])
    assert first_attempt.learner_id == second_attempt.learner_id


@override_settings(**_NODE_SETTINGS)
def test_post_submit_always_starts_a_fresh_attempt_against_the_current_active_package(
    quiz_world, keypair
):
    """The HTTP submit route starts+grades a brand-new Attempt inside one
    request (T15-D27 S11) -- GET never creates one, so there is no
    prior Attempt to stay pinned to. A POST issued after a newer package
    has since activated therefore correctly targets that new package: a
    FRESH Attempt always starts against whichever package is active right
    now. (The "an already-STARTED Attempt stays pinned even if a newer
    package later activates" invariant is a property of individual
    Attempt objects across two separate start/submit calls, and is
    covered directly at the service layer in
    linuxlab.learning.tests.test_installed_attempt_service.)"""
    private_key, public_key = keypair
    v1 = _stage(quiz_world, private_key, public_key)
    activate_package(v1)

    v2_snapshot = republish(quiz_world)
    v2_bytes = build_package(v2_snapshot, quiz_world["activity_registry"], private_key)
    v2 = import_package(v2_bytes, quiz_world["activity_registry"], public_key)
    activate_package(v2)

    response = Client().post(
        _submit_url(quiz_world), data=json.dumps(_correct_submission(quiz_world)), content_type="application/json"
    )

    assert response.status_code == 200
    attempt = Attempt.objects.get(public_id=response.json()["attempt_public_id"])
    assert attempt.installed_activity_version.package_id == v2.pk
