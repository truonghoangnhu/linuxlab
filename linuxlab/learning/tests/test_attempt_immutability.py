import threading

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

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


# --- F: ordinary save cannot perform the first-submission transition ------


@pytest.mark.django_db
def test_ordinary_save_cannot_first_submit_an_attempt(learner, published_version):
    from linuxlab.learning.models import Progress, Result

    attempt = start_attempt(learner, published_version)

    attempt.submission = {"anything": "value"}
    attempt.submitted_at = timezone.now()
    with pytest.raises(ValueError):
        attempt.save()

    reloaded = Attempt.objects.get(pk=attempt.pk)
    assert reloaded.submission is None
    assert reloaded.submitted_at is None
    assert Result.objects.filter(attempt=reloaded).count() == 0
    assert Progress.objects.filter(learner=learner, activity_definition=published_version.definition).exists() is False


# --- G: creation cannot fabricate already-submitted state ------------------


@pytest.mark.django_db
def test_create_with_submission_and_submitted_at_is_rejected(learner, published_version):
    with pytest.raises(ValueError):
        Attempt.objects.create(
            learner=learner,
            activity_version=published_version,
            submission={"x": 1},
            submitted_at=timezone.now(),
        )

    assert Attempt.objects.filter(learner=learner, activity_version=published_version).count() == 0


@pytest.mark.django_db
def test_create_with_only_submission_is_rejected(learner, published_version):
    with pytest.raises(ValueError):
        Attempt.objects.create(
            learner=learner,
            activity_version=published_version,
            submission={"x": 1},
        )

    assert Attempt.objects.filter(learner=learner, activity_version=published_version).count() == 0


@pytest.mark.django_db
def test_create_with_only_submitted_at_is_rejected(learner, published_version):
    with pytest.raises(ValueError):
        Attempt.objects.create(
            learner=learner,
            activity_version=published_version,
            submitted_at=timezone.now(),
        )

    assert Attempt.objects.filter(learner=learner, activity_version=published_version).count() == 0


# --- G2: explicit local PK is rejected regardless of pk is None -----------
# self.pk is None is NOT equivalent to Django's insertion state: an unsaved
# instance can have an explicit pk while self._state.adding is still True.


def _unused_pk():
    from django.db.models import Max

    current_max = Attempt.objects.aggregate(Max("pk"))["pk__max"] or 0
    return current_max + 1000


@pytest.mark.django_db
def test_explicit_unused_pk_with_fully_submitted_state_is_rejected(learner, published_version):
    from linuxlab.core.models import DomainEvent
    from linuxlab.learning.models import Progress, Result

    unused_pk = _unused_pk()
    attempt = Attempt(
        pk=unused_pk,
        learner=learner,
        activity_version=published_version,
        submission={"x": 1},
        submitted_at=timezone.now(),
    )
    assert attempt._state.adding is True
    assert attempt.pk == unused_pk

    with pytest.raises(ValueError):
        attempt.save()

    assert Attempt.objects.filter(pk=unused_pk).exists() is False
    assert Result.objects.count() == 0
    assert Progress.objects.filter(learner=learner, activity_definition=published_version.definition).exists() is False
    assert DomainEvent.objects.filter(event_type="activity.completed").count() == 0


@pytest.mark.django_db
def test_explicit_unused_pk_with_only_submission_is_rejected(learner, published_version):
    unused_pk = _unused_pk()
    attempt = Attempt(
        pk=unused_pk,
        learner=learner,
        activity_version=published_version,
        submission={"x": 1},
    )

    with pytest.raises(ValueError):
        attempt.save()

    assert Attempt.objects.filter(pk=unused_pk).exists() is False


@pytest.mark.django_db
def test_explicit_unused_pk_with_only_submitted_at_is_rejected(learner, published_version):
    unused_pk = _unused_pk()
    attempt = Attempt(
        pk=unused_pk,
        learner=learner,
        activity_version=published_version,
        submitted_at=timezone.now(),
    )

    with pytest.raises(ValueError):
        attempt.save()

    assert Attempt.objects.filter(pk=unused_pk).exists() is False


@pytest.mark.django_db
def test_explicit_unused_pk_even_when_unsubmitted_is_rejected(learner, published_version):
    # Local PK is a private implementation detail: ordinary application code
    # must never supply it, submitted or not.
    unused_pk = _unused_pk()
    attempt = Attempt(
        pk=unused_pk,
        learner=learner,
        activity_version=published_version,
    )

    with pytest.raises(ValueError):
        attempt.save()

    assert Attempt.objects.filter(pk=unused_pk).exists() is False


# --- G3: explicit EXISTING pk cannot overwrite another Attempt's history --


@pytest.mark.django_db
def test_explicit_existing_pk_cannot_overwrite_an_existing_attempt(
    learner, other_learner, published_version, other_published_version
):
    original = start_attempt(learner, published_version)

    impersonator = Attempt(
        pk=original.pk,
        learner=other_learner,
        activity_version=other_published_version,
    )
    assert impersonator._state.adding is True

    with pytest.raises(ValueError):
        impersonator.save()

    reloaded = Attempt.objects.get(pk=original.pk)
    assert reloaded.learner_id == learner.pk
    assert reloaded.activity_version_id == published_version.pk
    assert reloaded.submission is None
    assert reloaded.submitted_at is None


# --- G4: a missing persisted row must reject, not resurrect ---------------


@pytest.mark.django_db
def test_save_on_a_deleted_attempt_is_rejected_not_resurrected(learner, published_version):
    attempt = start_attempt(learner, published_version)
    stale = Attempt.objects.get(pk=attempt.pk)  # a second, separately-loaded copy

    Attempt.objects.filter(pk=attempt.pk).delete()

    with pytest.raises(ValueError):
        stale.save()

    assert Attempt.objects.filter(pk=attempt.pk).exists() is False


# --- H: submit_attempt() still performs its one legitimate transition -----


@pytest.mark.django_db
def test_submit_attempt_still_works_end_to_end_after_boundary_hardening(
    learner, published_version, registry
):
    from linuxlab.core.models import DomainEvent
    from linuxlab.learning.models import Progress, Result

    attempt = start_attempt(learner, published_version)
    result = submit_attempt(attempt, "ls", registry)

    reloaded = Attempt.objects.get(pk=attempt.pk)
    assert reloaded.submission == "ls"
    assert reloaded.submitted_at is not None

    assert Result.objects.filter(attempt=reloaded).count() == 1
    assert Result.objects.get(attempt=reloaded).pk == result.pk

    assert Progress.objects.filter(
        learner=learner, activity_definition=published_version.definition
    ).exists() is True

    assert (
        DomainEvent.objects.filter(
            event_type="activity.completed", aggregate_id=reloaded.public_id
        ).count()
        == 1
    )

    with pytest.raises(ValueError):
        submit_attempt(attempt, "ls", registry)


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
