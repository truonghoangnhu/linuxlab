import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version

Account = get_user_model()

CLAIM_URL = "/api/v1/learning/claim-progress/"


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


@pytest.fixture
def account(db):
    account = Account.objects.create(username="alice")
    account.set_password("irrelevant-for-this-test")
    account.save()
    return account


@pytest.fixture
def client():
    return Client()


def _claim_body(activity_version):
    return {
        "claims": [
            {
                "activity_public_id": str(activity_version.definition.public_id),
                "activity_version": activity_version.version,
            }
        ]
    }


# --- authentication gate ----------------------------------------------------


@pytest.mark.django_db
def test_claim_endpoint_requires_authentication(client, published_version):
    response = client.post(
        CLAIM_URL, data=json.dumps(_claim_body(published_version)), content_type="application/json"
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_authenticated_valid_claim_succeeds(client, account, published_version, definition):
    from linuxlab.learning.models import Progress

    client.force_login(account)

    response = client.post(
        CLAIM_URL, data=json.dumps(_claim_body(published_version)), content_type="application/json"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["claimed"] == 1

    from linuxlab.identity.models import Learner

    learner = Learner.objects.get(account=account)
    assert Progress.objects.get(learner=learner, activity_definition=definition).completed is True


@pytest.mark.django_db
def test_invalid_claim_is_rejected_with_400(client, account):
    client.force_login(account)

    body = {"claims": [{"activity_public_id": str(uuid.uuid4()), "activity_version": 1}]}
    response = client.post(CLAIM_URL, data=json.dumps(body), content_type="application/json")

    assert response.status_code == 400


@pytest.mark.django_db
def test_malformed_json_body_is_rejected_with_400(client, account):
    client.force_login(account)

    response = client.post(CLAIM_URL, data="not json", content_type="application/json")

    assert response.status_code == 400


@pytest.mark.django_db
def test_only_items_in_the_request_body_are_processed(
    client, account, published_version, definition
):
    # "unrelated anonymous browser data is not uploaded": the server only
    # ever sees and processes what is literally in the POST body.
    from linuxlab.activities.models import ActivityDefinition as ActivityDefinitionModel
    from linuxlab.learning.models import Progress

    other_definition = ActivityDefinitionModel.objects.create(name="Untouched activity", type_key="fake")

    client.force_login(account)
    response = client.post(
        CLAIM_URL, data=json.dumps(_claim_body(published_version)), content_type="application/json"
    )

    assert response.status_code == 200
    from linuxlab.identity.models import Learner

    learner = Learner.objects.get(account=account)
    assert Progress.objects.filter(learner=learner, activity_definition=other_definition).exists() is False


@pytest.mark.django_db
def test_repeated_claim_via_endpoint_is_idempotent(client, account, published_version):
    client.force_login(account)

    first = client.post(
        CLAIM_URL, data=json.dumps(_claim_body(published_version)), content_type="application/json"
    )
    second = client.post(
        CLAIM_URL, data=json.dumps(_claim_body(published_version)), content_type="application/json"
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["claimed"] == 1
    assert second.json()["claimed"] == 0

    from linuxlab.learning.models import Attempt

    assert Attempt.objects.filter(activity_version=published_version).count() == 1
