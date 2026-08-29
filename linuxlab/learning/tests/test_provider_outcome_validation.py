import pytest

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.core.models import DomainEvent
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Progress, Result
from linuxlab.learning.services import start_attempt, submit_attempt


class FixedOutcomeProvider:
    """Test-only provider: evaluate() returns whatever outcome the test
    hands it, so malformed provider output can be driven deterministically."""

    type_key = "fixed_outcome"

    def __init__(self, outcome):
        self.outcome = outcome

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return self.outcome


def _registry_for(outcome):
    reg = ActivityRegistry()
    reg.register(FixedOutcomeProvider(outcome))
    return reg


@pytest.fixture
def definition(db):
    return ActivityDefinition.objects.create(name="Fixed outcome activity", type_key="fixed_outcome")


@pytest.fixture
def learner(db):
    return Learner.objects.create(account=None)


def _published_version(definition, registry):
    version = create_activity_version(definition, config={})
    return publish_activity_version(version, registry)


MALFORMED_OUTCOMES = [
    pytest.param({}, id="missing_both_keys"),
    pytest.param({"score": True, "max_score": 1}, id="bool_score"),
    pytest.param({"score": "1", "max_score": 1}, id="string_score"),
    pytest.param({"score": 0.5, "max_score": 1}, id="float_score"),
    pytest.param({"score": -1, "max_score": 1}, id="negative_score"),
    pytest.param({"score": 2, "max_score": 1}, id="score_exceeds_max_score"),
]


@pytest.mark.django_db
@pytest.mark.parametrize("outcome", MALFORMED_OUTCOMES)
def test_malformed_provider_outcome_is_rejected_before_finalization(learner, definition, outcome):
    registry = _registry_for(outcome)
    published = _published_version(definition, registry)
    attempt = start_attempt(learner, published)

    with pytest.raises(ValueError):
        submit_attempt(attempt, {}, registry)

    attempt.refresh_from_db()
    assert attempt.submitted_at is None
    assert attempt.submission is None
    assert Result.objects.filter(attempt=attempt).count() == 0
    assert Progress.objects.filter(learner=learner, activity_definition=definition).exists() is False
    assert DomainEvent.objects.filter(event_type="activity.completed").count() == 0


@pytest.mark.django_db
def test_well_formed_integer_outcome_still_succeeds(learner, definition):
    registry = _registry_for({"score": 1, "max_score": 1})
    published = _published_version(definition, registry)
    attempt = start_attempt(learner, published)

    result = submit_attempt(attempt, {}, registry)

    assert result.score == 1
    assert result.max_score == 1
    attempt.refresh_from_db()
    assert attempt.submitted_at is not None
