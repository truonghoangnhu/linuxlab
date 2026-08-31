"""T15-D27 S6-S9: Node execution-source Attempt/Result/Progress lifecycle
against a real Task 09-12 chain (published assessment/single_choice
activity, staged and activated as an InstalledPackage) -- start against
the pinned InstalledActivityVersion, grade from installed artifacts only,
finalize through the same shared responsibilities as the Central path, and
never re-resolve "the current active package" for an already-started
Attempt."""

import pytest
from django.test import override_settings

from linuxlab.core.models import DomainEvent
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Progress
from linuxlab.learning.services import (
    start_installed_attempt,
    submit,
    submit_installed_attempt,
)
from linuxlab.runtime.context import open_runtime_context
from linuxlab.runtime.exceptions import RuntimeArtifactNotFound
from linuxlab.runtime.tests.conftest import republish
from linuxlab.sync.services import activate_package

pytestmark = pytest.mark.django_db(transaction=True)


def _context(world):
    package = world["package"]
    return open_runtime_context(package.package_type, package.source_public_id)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_start_installed_attempt_pins_the_installed_activity_version(installed_active_world):
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)

    attempt = start_installed_attempt(
        learner, context, world["definition"].public_id, world["activity_version"].version
    )

    assert attempt.activity_version_id is None
    assert attempt.installed_activity_version.activity_public_id == world["definition"].public_id
    assert attempt.installed_activity_version.version == world["activity_version"].version


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_start_installed_attempt_rejects_unknown_activity_in_package(installed_active_world):
    import uuid

    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)

    with pytest.raises(RuntimeArtifactNotFound):
        start_installed_attempt(learner, context, uuid.uuid4(), 1)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_submit_installed_attempt_grades_correct_response_from_installed_artifacts(
    installed_active_world,
):
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)
    attempt = start_installed_attempt(
        learner, context, world["definition"].public_id, world["activity_version"].version
    )
    submission = {
        "responses": [
            {
                "question_public_id": str(world["question"].public_id),
                "question_version": world["question_version"].version,
                "selected_option_id": "a",
            }
        ]
    }

    result = submit_installed_attempt(attempt, submission, world["activity_registry"])

    assert result.score == 1
    assert result.max_score == 1
    attempt.refresh_from_db()
    assert attempt.submitted_at is not None


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_submit_installed_attempt_grades_incorrect_response(installed_active_world):
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)
    attempt = start_installed_attempt(
        learner, context, world["definition"].public_id, world["activity_version"].version
    )
    submission = {
        "responses": [
            {
                "question_public_id": str(world["question"].public_id),
                "question_version": world["question_version"].version,
                "selected_option_id": "b",
            }
        ]
    }

    result = submit_installed_attempt(attempt, submission, world["activity_registry"])

    assert result.score == 0
    assert result.max_score == 1


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_submit_installed_attempt_does_not_expose_answer_key_in_result(installed_active_world):
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)
    attempt = start_installed_attempt(
        learner, context, world["definition"].public_id, world["activity_version"].version
    )
    submission = {
        "responses": [
            {
                "question_public_id": str(world["question"].public_id),
                "question_version": world["question_version"].version,
                "selected_option_id": "a",
            }
        ]
    }

    result = submit_installed_attempt(attempt, submission, world["activity_registry"])

    assert "answer_key" not in str(result.details).lower().replace("_", "")
    for question_result in result.details["results"]:
        assert "correct_option_id" not in question_result


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_submit_installed_attempt_refreshes_progress_keyed_by_stable_definition_public_id(
    installed_active_world,
):
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)
    attempt = start_installed_attempt(
        learner, context, world["definition"].public_id, world["activity_version"].version
    )
    submission = {
        "responses": [
            {
                "question_public_id": str(world["question"].public_id),
                "question_version": world["question_version"].version,
                "selected_option_id": "a",
            }
        ]
    }

    submit_installed_attempt(attempt, submission, world["activity_registry"])

    progress = Progress.objects.get(
        learner=learner, activity_definition_public_id=world["definition"].public_id
    )
    assert progress.completed is True
    assert progress.best_score == 1
    # Node never authors ActivityDefinition rows locally (T15-D27 S4.3).
    assert progress.activity_definition_id is None


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_submit_installed_attempt_emits_activity_completed_with_stable_definition_public_id(
    installed_active_world,
):
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)
    attempt = start_installed_attempt(
        learner, context, world["definition"].public_id, world["activity_version"].version
    )
    submission = {
        "responses": [
            {
                "question_public_id": str(world["question"].public_id),
                "question_version": world["question_version"].version,
                "selected_option_id": "a",
            }
        ]
    }

    submit_installed_attempt(attempt, submission, world["activity_registry"])

    event = DomainEvent.objects.get(event_type="activity.completed", aggregate_id=attempt.public_id)
    assert event.payload["activity_definition_public_id"] == str(world["definition"].public_id)
    assert event.payload["score"] == 1
    assert event.payload["max_score"] == 1


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_submit_installed_attempt_rejects_double_submission(installed_active_world):
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)
    attempt = start_installed_attempt(
        learner, context, world["definition"].public_id, world["activity_version"].version
    )
    submission = {
        "responses": [
            {
                "question_public_id": str(world["question"].public_id),
                "question_version": world["question_version"].version,
                "selected_option_id": "a",
            }
        ]
    }
    submit_installed_attempt(attempt, submission, world["activity_registry"])

    with pytest.raises(ValueError, match="already been submitted"):
        submit_installed_attempt(attempt, submission, world["activity_registry"])


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_dispatcher_submit_routes_installed_source_attempts(installed_active_world):
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)
    attempt = start_installed_attempt(
        learner, context, world["definition"].public_id, world["activity_version"].version
    )
    submission = {
        "responses": [
            {
                "question_public_id": str(world["question"].public_id),
                "question_version": world["question_version"].version,
                "selected_option_id": "a",
            }
        ]
    }

    result = submit(attempt, submission, world["activity_registry"])

    assert result.score == 1


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_existing_attempt_stays_pinned_to_its_package_after_a_newer_package_activates(
    installed_active_world,
):
    """T15-D27 S7: Package A active -> Attempt X starts against A. Package
    B later becomes active -> Attempt X remains pinned to A; submitting X
    must not re-resolve the current active package."""
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)
    attempt = start_installed_attempt(
        learner, context, world["definition"].public_id, world["activity_version"].version
    )
    pinned_package_pk = attempt.installed_activity_version.package_id

    # A NEW package version for the same source becomes active.
    from linuxlab.publication.package import build_package
    from linuxlab.sync.services import import_package

    new_snapshot = republish(world)
    new_package_bytes = build_package(new_snapshot, world["activity_registry"], world["private_key"])
    new_package = import_package(new_package_bytes, world["activity_registry"], world["public_key"])
    activate_package(new_package)

    submission = {
        "responses": [
            {
                "question_public_id": str(world["question"].public_id),
                "question_version": world["question_version"].version,
                "selected_option_id": "a",
            }
        ]
    }
    result = submit_installed_attempt(attempt, submission, world["activity_registry"])

    assert result.score == 1
    attempt.refresh_from_db()
    assert attempt.installed_activity_version.package_id == pinned_package_pk
    assert attempt.installed_activity_version.package_id != new_package.pk


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_new_attempt_cannot_start_against_a_since_archived_package(installed_active_world):
    """T15-D27 S6-S7: starting a NEW execution requires the underlying
    package to be currently ACTIVE. A context opened while its package was
    ACTIVE, then used to start an Attempt only after a newer package
    activation has since archived it, must be rejected -- unlike grading
    an EXISTING Attempt (which stays pinned regardless, see the sibling
    "stays pinned" test above), nothing NEW may start against a
    non-active package."""
    from linuxlab.publication.package import build_package
    from linuxlab.sync.services import import_package

    world = installed_active_world
    context = _context(world)  # opened while world["package"] is ACTIVE

    new_snapshot = republish(world)
    new_package_bytes = build_package(new_snapshot, world["activity_registry"], world["private_key"])
    new_package = import_package(new_package_bytes, world["activity_registry"], world["public_key"])
    activate_package(new_package)  # archives world["package"]

    learner = Learner.objects.create(account=None)
    with pytest.raises(ValueError, match="non-active package"):
        start_installed_attempt(
            learner, context, world["definition"].public_id, world["activity_version"].version
        )
