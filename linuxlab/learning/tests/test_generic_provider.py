"""Proves linuxlab.learning depends only on the ActivityProvider contract,
not on quiz/QuestionVersion/AssessmentActivityProvider (Task 07 requirement).
This test-only provider is intentionally not shipped in production code.
"""

import pytest

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Progress
from linuxlab.learning.services import start_attempt, submit_attempt


class EchoLengthProvider:
    """Awards one point per character in submission["text"] up to config["max_score"]."""

    type_key = "echo_length"

    def validate_config(self, config):
        if "max_score" not in config:
            raise ValueError("config must contain 'max_score'")

    def evaluate(self, config, submission):
        max_score = config["max_score"]
        score = min(len(submission.get("text", "")), max_score)
        return {"score": score, "max_score": max_score}


@pytest.fixture
def registry():
    reg = ActivityRegistry()
    reg.register(EchoLengthProvider())
    return reg


@pytest.fixture
def definition(db):
    return ActivityDefinition.objects.create(name="Echo length drill", type_key="echo_length")


@pytest.fixture
def learner(db):
    return Learner.objects.create(account=None)


@pytest.fixture
def published_version(definition, registry):
    version = create_activity_version(definition, config={"max_score": 5})
    return publish_activity_version(version, registry)


@pytest.mark.django_db
def test_generic_provider_flows_through_learning_state_without_quiz_coupling(
    learner, definition, published_version, registry
):
    attempt = start_attempt(learner, published_version)

    result = submit_attempt(attempt, {"text": "abc"}, registry)

    assert result.score == 3
    assert result.max_score == 5

    progress = Progress.objects.get(learner=learner, activity_definition=definition)
    assert progress.completed is False
    assert progress.best_score == 3
