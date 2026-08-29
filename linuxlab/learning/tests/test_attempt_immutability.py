import threading

import pytest
from django.db import close_old_connections, connection

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Attempt
from linuxlab.learning.services import start_attempt, submit_attempt


class FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        if "expected" not in config:
            raise ValueError("config must contain 'expected'")

    def evaluate(self, config, submission):
        correct = submission == config["expected"]
        return {"score": 1 if correct else 0, "max_score": 1}


@pytest.fixture
def registry():
    reg = ActivityRegistry()
    reg.register(FakeProvider())
    return reg


@pytest.fixture
def definition(db):
    return ActivityDefinition.objects.create(name="Fake activity", type_key="fake")


@pytest.fixture
def learner(db):
    return Learner.objects.create(account=None)


@pytest.fixture
def other_learner(db):
    return Learner.objects.create(account=None)


@pytest.fixture
def published_version(definition, registry):
    version = create_activity_version(definition, config={"expected": "ls"})
    return publish_activity_version(version, registry)


@pytest.fixture
def other_published_version(definition, registry):
    version = create_activity_version(definition, config={"expected": "pwd"})
    return publish_activity_version(version, registry)


# --- A: activity_version is immutable after creation ----------------------


@pytest.mark.django_db
def test_changing_activity_version_after_creation_is_rejected(
    learner, published_version, other_published_version
):
    attempt = start_attempt(learner, published_version)

    attempt.activity_version = other_published_version
    with pytest.raises(ValueError):
        attempt.save()

    reloaded = Attempt.objects.get(pk=attempt.pk)
    assert reloaded.activity_version_id == published_version.pk


# --- B: learner is immutable after creation --------------------------------


@pytest.mark.django_db
def test_changing_learner_after_creation_is_rejected(learner, other_learner, published_version):
    attempt = start_attempt(learner, published_version)

    attempt.learner = other_learner
    with pytest.raises(ValueError):
        attempt.save()

    reloaded = Attempt.objects.get(pk=attempt.pk)
    assert reloaded.learner_id == learner.pk


# --- C: submission is immutable after submission ---------------------------


@pytest.mark.django_db
def test_changing_submission_after_submit_is_rejected(learner, published_version, registry):
    attempt = start_attempt(learner, published_version)
    submit_attempt(attempt, "ls", registry)

    submitted = Attempt.objects.get(pk=attempt.pk)
    submitted.submission = "cheated"
    with pytest.raises(ValueError):
        submitted.save()

    reloaded = Attempt.objects.get(pk=attempt.pk)
    assert reloaded.submission == "ls"


# --- D: submitted_at is immutable after submission -------------------------


@pytest.mark.django_db
def test_changing_submitted_at_after_submit_is_rejected(learner, published_version, registry):
    attempt = start_attempt(learner, published_version)
    submit_attempt(attempt, "ls", registry)

    submitted = Attempt.objects.get(pk=attempt.pk)
    original_submitted_at = submitted.submitted_at
    submitted.submitted_at = None
    with pytest.raises(ValueError):
        submitted.save()

    reloaded = Attempt.objects.get(pk=attempt.pk)
    assert reloaded.submitted_at == original_submitted_at


# --- E: started_at is immutable after creation ------------------------------


@pytest.mark.django_db
def test_changing_started_at_after_creation_is_rejected(learner, published_version):
    import datetime

    attempt = start_attempt(learner, published_version)
    original_started_at = attempt.started_at

    attempt.started_at = original_started_at - datetime.timedelta(days=1)
    with pytest.raises(ValueError):
        attempt.save()

    reloaded = Attempt.objects.get(pk=attempt.pk)
    assert reloaded.started_at == original_started_at


# --- legitimate first-submission transition still works --------------------


@pytest.mark.django_db
def test_first_submission_transition_still_succeeds(learner, published_version, registry):
    attempt = start_attempt(learner, published_version)
    assert attempt.submitted_at is None

    submit_attempt(attempt, "ls", registry)

    reloaded = Attempt.objects.get(pk=attempt.pk)
    assert reloaded.submitted_at is not None
    assert reloaded.submission == "ls"


# --- stale pre-submit object cannot overwrite committed history -----------


@pytest.mark.django_db
def test_stale_in_memory_object_cannot_overwrite_committed_submission(
    learner, published_version, registry
):
    """The guard must check the LOCKED persisted row, not the caller's
    possibly-stale in-memory instance (Task 05/06 lesson)."""
    attempt = start_attempt(learner, published_version)
    stale = Attempt.objects.get(pk=attempt.pk)  # loaded before submission

    submit_attempt(attempt, "ls", registry)  # commits real history

    stale.submission = "tampered"
    with pytest.raises(ValueError):
        stale.save()

    reloaded = Attempt.objects.get(pk=attempt.pk)
    assert reloaded.submission == "ls"


class _FinalizeThenSignalWorker(threading.Thread):
    """Finalizes the attempt on its own DB connection, then signals the
    stale-mutation worker to proceed only after this commit is durable."""

    def __init__(self, attempt_pk, submission, registry, ready_to_finalize, finalized):
        super().__init__()
        self.attempt_pk = attempt_pk
        self.submission = submission
        self.registry = registry
        self.ready_to_finalize = ready_to_finalize
        self.finalized = finalized

    def run(self):
        close_old_connections()
        try:
            attempt = Attempt.objects.get(pk=self.attempt_pk)
            self.ready_to_finalize.wait()
            submit_attempt(attempt, self.submission, self.registry)
        finally:
            connection.close()
            self.finalized.set()


class _StaleMutationAfterCommitWorker(threading.Thread):
    """Holds an Attempt copy loaded before finalization and, only after the
    other worker's commit is durable, attempts to mutate historical fields
    directly -- reproducing the exact stale-write race the review flagged."""

    def __init__(self, attempt_pk, stale_submission, ready_to_finalize, finalized, outcome):
        super().__init__()
        self.attempt_pk = attempt_pk
        self.stale_submission = stale_submission
        self.ready_to_finalize = ready_to_finalize
        self.finalized = finalized
        self.outcome = outcome

    def run(self):
        close_old_connections()
        try:
            stale = Attempt.objects.get(pk=self.attempt_pk)
            self.ready_to_finalize.set()
            self.finalized.wait()
            try:
                stale.submission = self.stale_submission
                stale.save()
                self.outcome.append("success")
            except ValueError:
                self.outcome.append("rejected")
        finally:
            connection.close()


@pytest.mark.django_db(transaction=True)
def test_stale_attempt_cannot_overwrite_committed_history_across_connections():
    definition = ActivityDefinition.objects.create(name="Fake activity", type_key="fake")
    registry = ActivityRegistry()
    registry.register(FakeProvider())
    version = create_activity_version(definition, config={"expected": "ls"})
    published = publish_activity_version(version, registry)
    learner = Learner.objects.create(account=None)
    attempt = start_attempt(learner, published)

    ready_to_finalize = threading.Event()
    finalized = threading.Event()
    outcome = []

    stale_worker = _StaleMutationAfterCommitWorker(
        attempt.pk, "tampered", ready_to_finalize, finalized, outcome
    )
    finalize_worker = _FinalizeThenSignalWorker(attempt.pk, "ls", registry, ready_to_finalize, finalized)

    stale_worker.start()
    finalize_worker.start()
    stale_worker.join()
    finalize_worker.join()

    assert outcome == ["rejected"]
    reloaded = Attempt.objects.get(pk=attempt.pk)
    assert reloaded.submission == "ls"
