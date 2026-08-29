import threading

import pytest
from django.db import close_old_connections, connection

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Learner
from linuxlab.learning.services import claim_activity_completion


class FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": 1, "max_score": 1}


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
def published_version(definition, registry):
    version = create_activity_version(definition, config={})
    return publish_activity_version(version, registry)


@pytest.mark.django_db
def test_claim_activity_completion_rejects_unpublished_activity_version(learner, definition):
    unpublished = create_activity_version(definition, config={})

    with pytest.raises(ValueError):
        claim_activity_completion(learner, unpublished)


# --- valid claim creates authoritative, honest history --------------------


@pytest.mark.django_db
def test_claim_creates_attempt_and_result_owned_by_the_claiming_learner(learner, published_version):
    from linuxlab.learning.models import Attempt, Result

    result = claim_activity_completion(learner, published_version)

    assert result is not None
    assert Attempt.objects.filter(learner=learner, activity_version=published_version).count() == 1
    attempt = Attempt.objects.get(learner=learner, activity_version=published_version)
    assert Result.objects.get(attempt=attempt).pk == result.pk


@pytest.mark.django_db
def test_claim_result_is_honest_about_self_attested_provenance(learner, published_version):
    result = claim_activity_completion(learner, published_version)

    assert result.details["source"] == "guest_claim"
    assert result.score == 1
    assert result.max_score == 1


@pytest.mark.django_db
def test_claim_refreshes_progress(learner, definition, published_version):
    from linuxlab.learning.models import Progress

    claim_activity_completion(learner, published_version)

    progress = Progress.objects.get(learner=learner, activity_definition=definition)
    assert progress.completed is True
    assert progress.best_score == 1


@pytest.mark.django_db
def test_claim_emits_activity_completed_domain_event(learner, published_version):
    from linuxlab.core.models import DomainEvent
    from linuxlab.learning.models import Attempt

    claim_activity_completion(learner, published_version)

    attempt = Attempt.objects.get(learner=learner, activity_version=published_version)
    events = DomainEvent.objects.filter(event_type="activity.completed", aggregate_id=attempt.public_id)
    assert events.count() == 1
    assert events.first().payload["source"] == "guest_claim"


# --- idempotency: repeating the same claim is a safe no-op ----------------


@pytest.mark.django_db
def test_repeating_the_same_claim_is_idempotent(learner, definition, published_version):
    from linuxlab.core.models import DomainEvent
    from linuxlab.learning.models import Attempt, Result

    first = claim_activity_completion(learner, published_version)
    second = claim_activity_completion(learner, published_version)

    assert first is not None
    assert second is None
    assert Attempt.objects.filter(learner=learner, activity_version=published_version).count() == 1
    assert Result.objects.filter(attempt__learner=learner).count() == 1
    assert (
        DomainEvent.objects.filter(event_type="activity.completed", aggregate_id__isnull=False).count() == 1
    )


# --- history preservation: no mutation of pre-existing records ------------


@pytest.mark.django_db
def test_claim_never_mutates_an_unrelated_pre_existing_attempt(
    learner, published_version, registry
):
    from linuxlab.learning.services import start_attempt, submit_attempt

    real_attempt = start_attempt(learner, published_version)
    real_result = submit_attempt(real_attempt, {"anything": True}, registry)

    claim_activity_completion(learner, published_version)

    real_attempt.refresh_from_db()
    assert real_attempt.submission == {"anything": True}
    real_result.refresh_from_db()
    assert real_result.details == {"score": 1, "max_score": 1}


@pytest.mark.django_db
def test_claim_does_not_affect_a_different_learners_progress(
    learner, definition, published_version
):
    from linuxlab.learning.models import Progress

    other_learner = Learner.objects.create(account=None)

    claim_activity_completion(learner, published_version)

    assert Progress.objects.filter(learner=other_learner, activity_definition=definition).exists() is False


# --- delete + rebuild proof (Task 07 projection invariant) ----------------


@pytest.mark.django_db
def test_claimed_progress_is_rebuildable_from_authoritative_history(learner, definition, published_version):
    from linuxlab.learning.models import Progress
    from linuxlab.learning.projections import rebuild_progress

    claim_activity_completion(learner, published_version)
    expected = Progress.objects.get(learner=learner, activity_definition=definition)
    expected_completed, expected_best_score = expected.completed, expected.best_score

    Progress.objects.filter(pk=expected.pk).delete()
    rebuild_progress()

    rebuilt = Progress.objects.get(learner=learner, activity_definition=definition)
    assert rebuilt.completed == expected_completed
    assert rebuilt.best_score == expected_best_score


# --- concurrency: two concurrent identical claims for the same learner ----


class _ClaimWorker(threading.Thread):
    def __init__(self, learner_pk, activity_version_pk, barrier, results, index):
        super().__init__()
        self.learner_pk = learner_pk
        self.activity_version_pk = activity_version_pk
        self.barrier = barrier
        self.results = results
        self.index = index

    def run(self):
        close_old_connections()
        try:
            from linuxlab.activities.models import ActivityVersion as ActivityVersionModel
            from linuxlab.identity.models import Learner as LearnerModel

            learner = LearnerModel.objects.get(pk=self.learner_pk)
            activity_version = ActivityVersionModel.objects.get(pk=self.activity_version_pk)
            self.barrier.wait()
            result = claim_activity_completion(learner, activity_version)
            self.results[self.index] = "created" if result is not None else "no_op"
        finally:
            connection.close()


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_claims_for_same_learner_yield_exactly_one_result():
    from linuxlab.learning.models import Result

    definition = ActivityDefinition.objects.create(name="Fake activity", type_key="fake")
    registry = ActivityRegistry()
    registry.register(FakeProvider())
    version = create_activity_version(definition, config={})
    published = publish_activity_version(version, registry)
    learner = Learner.objects.create(account=None)

    barrier = threading.Barrier(2)
    results = [None, None]
    workers = [_ClaimWorker(learner.pk, published.pk, barrier, results, i) for i in range(2)]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert results.count("created") == 1
    assert results.count("no_op") == 1
    assert Result.objects.filter(attempt__learner=learner, details__source="guest_claim").count() == 1
