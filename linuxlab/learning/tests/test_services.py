import pytest

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Attempt
from linuxlab.learning.services import start_attempt


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


# --- start_attempt: published-version requirement ---------------------------


@pytest.mark.django_db
def test_start_attempt_rejects_unpublished_activity_version(learner, definition):
    unpublished = create_activity_version(definition, config={"expected": "ls"})

    with pytest.raises(ValueError):
        start_attempt(learner, unpublished)

    assert Attempt.objects.count() == 0


@pytest.mark.django_db
def test_start_attempt_stores_exact_published_version(learner, published_version):
    attempt = start_attempt(learner, published_version)

    assert attempt.activity_version_id == published_version.pk
    assert attempt.learner_id == learner.pk
