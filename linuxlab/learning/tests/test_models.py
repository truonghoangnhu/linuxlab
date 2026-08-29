import uuid

import pytest
from django.db import IntegrityError

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Attempt, Result
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


# --- Attempt identity ---------------------------------------------------


@pytest.mark.django_db
def test_attempt_has_stable_uuid_public_id(learner, published_version):
    attempt = start_attempt(learner, published_version)

    assert isinstance(attempt.public_id, uuid.UUID)


@pytest.mark.django_db
def test_attempt_retains_local_integer_primary_key(learner, published_version):
    attempt = start_attempt(learner, published_version)

    assert isinstance(attempt.pk, int)
    assert attempt.pk != attempt.public_id


@pytest.mark.django_db
def test_two_attempts_may_reference_the_same_activity_version(learner, published_version):
    first = start_attempt(learner, published_version)
    second = start_attempt(learner, published_version)

    assert first.pk != second.pk
    assert first.activity_version_id == second.activity_version_id


@pytest.mark.django_db
def test_attempt_does_not_follow_a_newer_activity_version(
    learner, definition, registry, published_version
):
    attempt = start_attempt(learner, published_version)

    newer_version = create_activity_version(definition, config={"expected": "pwd"})
    publish_activity_version(newer_version, registry)

    attempt.refresh_from_db()
    assert attempt.activity_version.version == 1
    assert attempt.activity_version_id == published_version.pk


# --- Result immutability and cardinality --------------------------------


@pytest.mark.django_db
def test_result_references_its_attempt(learner, published_version):
    attempt = start_attempt(learner, published_version)
    result = Result.objects.create(attempt=attempt, score=1, max_score=1, details={"correct": True})

    assert result.attempt_id == attempt.pk


@pytest.mark.django_db
def test_second_result_for_the_same_attempt_is_rejected(learner, published_version):
    attempt = start_attempt(learner, published_version)
    Result.objects.create(attempt=attempt, score=1, max_score=1, details={"correct": True})

    with pytest.raises(IntegrityError):
        Result.objects.create(attempt=attempt, score=0, max_score=1, details={"correct": False})


@pytest.mark.django_db
def test_result_cannot_be_mutated_through_normal_save(learner, published_version):
    attempt = start_attempt(learner, published_version)
    result = Result.objects.create(attempt=attempt, score=1, max_score=1, details={"correct": True})

    result.score = 0
    with pytest.raises(ValueError):
        result.save()

    reloaded = Result.objects.get(pk=result.pk)
    assert reloaded.score == 1


@pytest.mark.django_db
def test_retry_creates_a_new_attempt_rather_than_mutating_prior_result(learner, published_version):
    first_attempt = start_attempt(learner, published_version)
    Result.objects.create(attempt=first_attempt, score=0, max_score=1, details={"correct": False})

    second_attempt = start_attempt(learner, published_version)
    Result.objects.create(attempt=second_attempt, score=1, max_score=1, details={"correct": True})

    assert Attempt.objects.filter(learner=learner, activity_version=published_version).count() == 2
    first_result = Result.objects.get(attempt=first_attempt)
    assert first_result.score == 0
