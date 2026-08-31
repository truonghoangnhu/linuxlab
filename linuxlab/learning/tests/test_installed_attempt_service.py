"""T15-D27 S6-S9: Node execution-source Attempt/Result/Progress lifecycle
against a real Task 09-12 chain (published assessment/single_choice
activity, staged and activated as an InstalledPackage) -- start against
the pinned InstalledActivityVersion, grade from installed artifacts only,
finalize through the same shared responsibilities as the Central path, and
never re-resolve "the current active package" for an already-started
Attempt."""

import threading
import time
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from django.test import override_settings

from linuxlab.core.models import DomainEvent
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Attempt, Progress
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
    with pytest.raises(ValueError, match="not active"):
        start_installed_attempt(
            learner, context, world["definition"].public_id, world["activity_version"].version
        )


# --- T15-D27 corrective: genuine concurrent race between attempt-start and
# --- activation, not merely the sequential "already archived" case above --


class _AttemptStarterWorker(threading.Thread):
    """Starts an installed Attempt on its own DB connection. Attempt.objects
    .create() is patched (with wraps=, so real behavior is unchanged) to
    signal `lock_acquired` and then block on `release_insert` immediately
    before the actual INSERT -- this is the exact point at which, if
    start_installed_attempt() correctly holds the same lifecycle lock
    activate_package() uses, a concurrent activation of a different
    package for the same source MUST be blocked too."""

    def __init__(self, learner, context, activity_public_id, version, lock_acquired, release_insert, results):
        super().__init__()
        self.learner = learner
        self.context = context
        self.activity_public_id = activity_public_id
        self.version = version
        self.lock_acquired = lock_acquired
        self.release_insert = release_insert
        self.results = results

    def run(self):
        close_old_connections()
        try:
            real_create = Attempt.objects.create

            def _signal_then_create(*args, **kwargs):
                self.lock_acquired.set()
                self.release_insert.wait(timeout=5)
                return real_create(*args, **kwargs)

            with patch.object(Attempt.objects, "create", side_effect=_signal_then_create):
                attempt = start_installed_attempt(
                    self.learner, self.context, self.activity_public_id, self.version
                )
            self.results["attempt_pk"] = attempt.pk
        finally:
            connection.close()


class _ConcurrentActivatorWorker(threading.Thread):
    """Waits for the starter to signal that it holds the lock, then
    attempts to activate a DIFFERENT package for the SAME source. If
    start_installed_attempt() holds the correct lifecycle lock, this call
    must block until the starter's transaction commits or rolls back --
    recorded here via a wall-clock timestamp, not just eventual success,
    since an unlocked implementation would also eventually "succeed" just
    without ever actually blocking."""

    def __init__(self, package, lock_acquired, results):
        super().__init__()
        self.package = package
        self.lock_acquired = lock_acquired
        self.results = results

    def run(self):
        close_old_connections()
        try:
            self.lock_acquired.wait(timeout=5)
            activate_package(self.package)
            self.results["activate_finished_at"] = time.monotonic()
        finally:
            connection.close()


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_concurrent_activation_cannot_race_a_new_attempt_start(installed_active_world):
    """T15-D27 corrective (Important finding): starting a new installed
    Attempt and activating a different package for the SAME source must
    serialize on the same lifecycle lock activate_package() itself uses --
    never let a concurrent activation archive the package a new Attempt is
    in the middle of pinning to."""
    from linuxlab.publication.package import build_package
    from linuxlab.sync.services import import_package

    world = installed_active_world
    learner = Learner.objects.create(account=None)
    context = _context(world)  # opened while world["package"] (A) is ACTIVE

    new_snapshot = republish(world)
    new_package_bytes = build_package(new_snapshot, world["activity_registry"], world["private_key"])
    package_b = import_package(new_package_bytes, world["activity_registry"], world["public_key"])

    lock_acquired = threading.Event()
    release_insert = threading.Event()
    results = {}

    starter = _AttemptStarterWorker(
        learner, context, world["definition"].public_id, world["activity_version"].version,
        lock_acquired, release_insert, results,
    )
    activator = _ConcurrentActivatorWorker(package_b, lock_acquired, results)

    try:
        starter.start()
        activator.start()

        assert lock_acquired.wait(timeout=5), "starter never reached the pre-insert signal point"
        # The activator has already been released to attempt activate_package();
        # give it a real chance to actually reach (and block on) the lock
        # before releasing the starter -- otherwise this proves nothing.
        time.sleep(0.3)
        # Record the fact rather than asserting immediately: an unlocked
        # implementation lets the activator finish here, and asserting
        # right away (raising before release_insert.set() below runs)
        # would leave the starter thread permanently blocked past this
        # test's teardown, corrupting a LATER test instead of this one.
        activated_too_early = "activate_finished_at" in results
    finally:
        released_at = time.monotonic()
        release_insert.set()
        starter.join(timeout=5)
        activator.join(timeout=5)

    assert not activated_too_early, (
        "activate_package() completed before the starter's transaction "
        "committed -- it was not actually blocked by the same lifecycle lock"
    )
    assert "attempt_pk" in results
    assert "activate_finished_at" in results
    assert results["activate_finished_at"] >= released_at

    attempt = Attempt.objects.get(pk=results["attempt_pk"])
    assert attempt.installed_activity_version.package_id == world["package"].pk


# --- T15-D27 corrective (Task 5): installed finalization is one atomic
# --- rollback boundary, not a sequence of independently-committed writes --


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_submit_installed_attempt_rolls_back_everything_on_late_failure(installed_active_world):
    """A failure inside _finalize_submission() AFTER Result/Progress/
    DomainEvent have all been written -- here, a genuine operational
    misconfiguration (sync enabled with no Node identity configured,
    which enqueue_sync_event() itself already rejects) -- must roll back
    the WHOLE submission as one unit: no Result, no Progress row, no
    DomainEvent, and the Attempt itself remains unsubmitted, exactly as
    if submit_installed_attempt() had never been called at all."""
    from linuxlab.sync.enqueue import SyncConfigurationError

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

    with (
        override_settings(LINUXLAB_SYNC_ENABLED=True, LINUXLAB_NODE_ID=None),
        pytest.raises(SyncConfigurationError),
    ):
        submit_installed_attempt(attempt, submission, world["activity_registry"])

    attempt.refresh_from_db()
    assert attempt.submitted_at is None
    assert attempt.submission is None
    assert not hasattr(attempt, "result")
    assert Progress.objects.filter(
        learner=learner, activity_definition_public_id=world["definition"].public_id
    ).exists() is False
    assert not DomainEvent.objects.filter(
        event_type="activity.completed", aggregate_id=attempt.public_id
    ).exists()

    # The attempt is genuinely still submittable afterward -- the failed
    # attempt left no partial state behind for a retry to trip over.
    with override_settings(LINUXLAB_SYNC_ENABLED=False):
        result = submit_installed_attempt(attempt, submission, world["activity_registry"])
    assert result.score == 1
