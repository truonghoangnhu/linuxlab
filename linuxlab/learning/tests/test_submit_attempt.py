import pytest

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.core.models import DomainEvent
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Progress, Result
from linuxlab.learning.services import start_attempt, submit_attempt


class FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        if "expected" not in config:
            raise ValueError("config must contain 'expected'")

    def evaluate(self, config, submission):
        correct = submission == config["expected"]
        return {"score": 1 if correct else 0, "max_score": 1, "correct": correct}


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
    version = create_activity_version(definition, config={"expected": "ls"})
    return publish_activity_version(version, registry)


@pytest.mark.django_db
def test_submit_attempt_persists_result_from_provider_evaluation(learner, published_version, registry):
    attempt = start_attempt(learner, published_version)

    result = submit_attempt(attempt, "ls", registry)

    assert result.attempt_id == attempt.pk
    assert result.score == 1
    assert result.max_score == 1
    assert result.details == {"score": 1, "max_score": 1, "correct": True}


@pytest.mark.django_db
def test_submit_attempt_stores_submission_and_submitted_at(learner, published_version, registry):
    attempt = start_attempt(learner, published_version)

    submit_attempt(attempt, "ls", registry)

    attempt.refresh_from_db()
    assert attempt.submission == "ls"
    assert attempt.submitted_at is not None


@pytest.mark.django_db
def test_submit_attempt_rejects_double_submission(learner, published_version, registry):
    attempt = start_attempt(learner, published_version)
    submit_attempt(attempt, "ls", registry)

    with pytest.raises(ValueError):
        submit_attempt(attempt, "ls", registry)

    assert Result.objects.filter(attempt=attempt).count() == 1


@pytest.mark.django_db
def test_submit_attempt_emits_activity_completed_domain_event(learner, published_version, registry):
    attempt = start_attempt(learner, published_version)

    submit_attempt(attempt, "ls", registry)

    events = DomainEvent.objects.filter(event_type="activity.completed", aggregate_id=attempt.public_id)
    assert events.count() == 1
    payload = events.first().payload
    assert payload["score"] == 1
    assert payload["max_score"] == 1


@pytest.mark.django_db
def test_submit_attempt_refreshes_progress(learner, published_version, registry):
    attempt = start_attempt(learner, published_version)

    submit_attempt(attempt, "ls", registry)

    progress = Progress.objects.get(learner=learner, activity_definition=published_version.definition)
    assert progress.completed is True
    assert progress.best_score == 1
