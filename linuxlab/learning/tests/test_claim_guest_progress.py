import uuid

import pytest
from django.contrib.auth import get_user_model

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Learner
from linuxlab.learning.services import claim_guest_progress

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
def second_definition(db):
    return ActivityDefinition.objects.create(name="Second fake activity", type_key="fake")


@pytest.fixture
def published_version(definition, registry):
    version = create_activity_version(definition, config={})
    return publish_activity_version(version, registry)


@pytest.fixture
def second_published_version(second_definition, registry):
    version = create_activity_version(second_definition, config={})
    return publish_activity_version(version, registry)


@pytest.fixture
def account(db):
    return Account.objects.create(username="alice")


def _claim_item(activity_version):
    return {
        "activity_public_id": str(activity_version.definition.public_id),
        "activity_version": activity_version.version,
    }


# --- validation: every item must resolve to a published ActivityVersion ---


@pytest.mark.django_db
def test_claim_rejects_unknown_activity_public_id(account):
    with pytest.raises(ValueError):
        claim_guest_progress(account, [{"activity_public_id": str(uuid.uuid4()), "activity_version": 1}])

    assert Learner.objects.filter(account=account).exists() is False


@pytest.mark.django_db
def test_claim_rejects_unpublished_version(account, definition):
    create_activity_version(definition, config={})

    with pytest.raises(ValueError):
        claim_guest_progress(
            account, [{"activity_public_id": str(definition.public_id), "activity_version": 1}]
        )


@pytest.mark.django_db
def test_claim_rejects_empty_claims_list(account):
    with pytest.raises(ValueError):
        claim_guest_progress(account, [])


@pytest.mark.django_db
def test_one_invalid_item_rejects_the_whole_batch(account, published_version, definition):
    from linuxlab.learning.models import Attempt

    claims = [_claim_item(published_version), {"activity_public_id": str(uuid.uuid4()), "activity_version": 1}]

    with pytest.raises(ValueError):
        claim_guest_progress(account, claims)

    assert Attempt.objects.count() == 0


# --- success path -----------------------------------------------------------


@pytest.mark.django_db
def test_valid_claim_resolves_learner_and_creates_history(account, published_version, definition):
    from linuxlab.learning.models import Attempt, Progress

    learner, results = claim_guest_progress(account, [_claim_item(published_version)])

    assert learner.account_id == account.pk
    assert len(results) == 1
    assert Attempt.objects.filter(learner=learner, activity_version=published_version).count() == 1
    assert Progress.objects.get(learner=learner, activity_definition=definition).completed is True


@pytest.mark.django_db
def test_claim_processes_multiple_valid_items(
    account, published_version, second_published_version, definition, second_definition
):
    from linuxlab.learning.models import Progress

    learner, results = claim_guest_progress(
        account, [_claim_item(published_version), _claim_item(second_published_version)]
    )

    assert len(results) == 2
    assert Progress.objects.get(learner=learner, activity_definition=definition).completed is True
    assert Progress.objects.get(learner=learner, activity_definition=second_definition).completed is True


@pytest.mark.django_db
def test_claim_is_idempotent_across_repeated_calls(account, published_version):
    from linuxlab.learning.models import Attempt

    claim_guest_progress(account, [_claim_item(published_version)])
    learner, second_results = claim_guest_progress(account, [_claim_item(published_version)])

    assert second_results == []
    assert Attempt.objects.filter(learner=learner, activity_version=published_version).count() == 1


@pytest.mark.django_db
def test_only_explicitly_claimed_items_are_processed(
    account, published_version, second_published_version, definition, second_definition
):
    # "unrelated anonymous browser data is not uploaded": the service only
    # ever sees and acts on what is literally in the claims list.
    from linuxlab.learning.models import Progress

    learner, results = claim_guest_progress(account, [_claim_item(published_version)])

    assert len(results) == 1
    assert Progress.objects.filter(learner=learner, activity_definition=second_definition).exists() is False
