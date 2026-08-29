"""Guest-first coverage (ADR-006, ADR-012) tied to the Task 08 claim flow.

Task 07's suite already exercises Learner(account=None) throughout every
start_attempt/submit_attempt/Progress path; this file specifically proves
the end-to-end guest lifecycle Task 08 adds: a learner can do real work with
zero Account involvement, and only claim requires authentication.
"""

import pytest
from django.contrib.auth import get_user_model

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Progress, Result
from linuxlab.learning.services import claim_activity_completion, start_attempt, submit_attempt

Account = get_user_model()


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
def published_version(definition, registry):
    version = create_activity_version(definition, config={})
    return publish_activity_version(version, registry)


@pytest.mark.django_db
def test_guest_learner_exists_without_an_account():
    guest = Learner.objects.create(account=None)

    assert guest.account is None


@pytest.mark.django_db
def test_guest_can_start_and_submit_an_attempt_with_no_account(published_version, registry):
    guest = Learner.objects.create(account=None)

    attempt = start_attempt(guest, published_version)
    result = submit_attempt(attempt, {"anything": True}, registry)

    assert result.score == 1


@pytest.mark.django_db
def test_guest_progress_derives_with_no_account(published_version, definition, registry):
    guest = Learner.objects.create(account=None)
    attempt = start_attempt(guest, published_version)
    submit_attempt(attempt, {"anything": True}, registry)

    progress = Progress.objects.get(learner=guest, activity_definition=definition)
    assert progress.completed is True


@pytest.mark.django_db
def test_claim_activity_completion_does_not_require_any_account(published_version, definition):
    # claim_activity_completion() itself only needs a Learner -- it is
    # claim_guest_progress()/the HTTP endpoint that requires authentication,
    # not the underlying learning-state primitive.
    guest = Learner.objects.create(account=None)

    result = claim_activity_completion(guest, published_version)

    assert result is not None
    assert Result.objects.filter(attempt__learner=guest).count() == 1
