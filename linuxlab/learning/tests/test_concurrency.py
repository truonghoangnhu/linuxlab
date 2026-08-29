import threading

import pytest
from django.db import close_old_connections, connection

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Progress, Result
from linuxlab.learning.projections import compute_progress_values
from linuxlab.learning.services import start_attempt, submit_attempt


class ScoredProvider:
    type_key = "scored"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": submission["score"], "max_score": submission["max_score"]}


# --- same Attempt, concurrent finalization --------------------------------


class _SubmitWorker(threading.Thread):
    """Submits the same attempt on its own DB connection, synchronized by a
    barrier so both workers enter submit_attempt() together."""

    def __init__(self, attempt_pk, submission, registry, barrier, results, index):
        super().__init__()
        self.attempt_pk = attempt_pk
        self.submission = submission
        self.registry = registry
        self.barrier = barrier
        self.results = results
        self.index = index

    def run(self):
        close_old_connections()
        try:
            from linuxlab.learning.models import Attempt

            attempt = Attempt.objects.get(pk=self.attempt_pk)
            self.barrier.wait()
            try:
                submit_attempt(attempt, self.submission, self.registry)
                self.results[self.index] = "success"
            except ValueError:
                self.results[self.index] = "rejected"
        finally:
            connection.close()


@pytest.mark.django_db(transaction=True)
def test_concurrent_finalization_of_the_same_attempt_yields_exactly_one_result():
    definition = ActivityDefinition.objects.create(name="Scored activity", type_key="scored")
    registry = ActivityRegistry()
    registry.register(ScoredProvider())
    version = create_activity_version(definition, config={})
    published = publish_activity_version(version, registry)
    learner = Learner.objects.create(account=None)
    attempt = start_attempt(learner, published)

    barrier = threading.Barrier(2)
    results = [None, None]
    workers = [
        _SubmitWorker(
            attempt.pk, {"score": 1, "max_score": 1}, registry, barrier, results, i
        )
        for i in range(2)
    ]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert results.count("success") == 1
    assert results.count("rejected") == 1
    assert Result.objects.filter(attempt=attempt).count() == 1

    progress = Progress.objects.get(learner=learner, activity_definition=definition)
    assert progress.completed is True
    assert progress.best_score == 1


# --- different Attempts, same Progress key, concurrent completion --------


class _NewAttemptSubmitWorker(threading.Thread):
    """Starts and submits its OWN attempt on its own DB connection,
    synchronized by a barrier so both workers submit_attempt() together
    against the same (learner, activity_definition) Progress key."""

    def __init__(self, learner_pk, activity_version_pk, submission, registry, barrier):
        super().__init__()
        self.learner_pk = learner_pk
        self.activity_version_pk = activity_version_pk
        self.submission = submission
        self.registry = registry
        self.barrier = barrier

    def run(self):
        close_old_connections()
        try:
            from linuxlab.activities.models import ActivityVersion
            from linuxlab.identity.models import Learner as LearnerModel

            learner = LearnerModel.objects.get(pk=self.learner_pk)
            activity_version = ActivityVersion.objects.get(pk=self.activity_version_pk)
            attempt = start_attempt(learner, activity_version)
            self.barrier.wait()
            submit_attempt(attempt, self.submission, self.registry)
        finally:
            connection.close()


@pytest.mark.django_db(transaction=True)
def test_concurrent_attempts_for_the_same_progress_key_equal_a_rebuild():
    definition = ActivityDefinition.objects.create(name="Scored activity", type_key="scored")
    registry = ActivityRegistry()
    registry.register(ScoredProvider())
    version = create_activity_version(definition, config={})
    published = publish_activity_version(version, registry)
    learner = Learner.objects.create(account=None)

    barrier = threading.Barrier(2)
    workers = [
        _NewAttemptSubmitWorker(
            learner.pk, published.pk, {"score": 1, "max_score": 2}, registry, barrier
        ),
        _NewAttemptSubmitWorker(
            learner.pk, published.pk, {"score": 2, "max_score": 2}, registry, barrier
        ),
    ]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert Result.objects.filter(attempt__learner=learner).count() == 2

    progress = Progress.objects.get(learner=learner, activity_definition=definition)

    results = list(
        Result.objects.filter(
            attempt__learner=learner, attempt__activity_version__definition=definition
        ).select_related("attempt__activity_version")
    )
    expected_completed, expected_best_score = compute_progress_values(results)

    assert progress.completed == expected_completed
    assert progress.best_score == expected_best_score
    assert Progress.objects.filter(learner=learner, activity_definition=definition).count() == 1
