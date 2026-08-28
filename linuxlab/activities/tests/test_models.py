import uuid

import pytest
from django.db import IntegrityError

from linuxlab.activities.models import ActivityDefinition, ActivityVersion


@pytest.mark.django_db
def test_activity_definition_receives_uuid_public_id():
    definition = ActivityDefinition.objects.create(name="Shell basics quiz", type_key="assessment")

    assert isinstance(definition.public_id, uuid.UUID)


@pytest.mark.django_db
def test_two_activity_definitions_receive_distinct_public_ids():
    first = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")
    second = ActivityDefinition.objects.create(name="Quiz B", type_key="assessment")

    assert first.public_id != second.public_id


@pytest.mark.django_db
def test_activity_definition_retains_local_integer_primary_key():
    definition = ActivityDefinition.objects.create(name="Shell basics quiz", type_key="assessment")

    assert isinstance(definition.pk, int)
    assert str(definition.public_id) != str(definition.pk)


@pytest.mark.django_db
def test_multiple_definitions_may_share_the_same_type_key():
    first = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")
    second = ActivityDefinition.objects.create(name="Quiz B", type_key="assessment")

    assert first.type_key == second.type_key == "assessment"


@pytest.mark.django_db
def test_first_activity_version_for_a_definition_can_be_created_directly():
    definition = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")

    version = ActivityVersion.objects.create(definition=definition, version=1)

    assert version.version == 1
    assert version.published_at is None


@pytest.mark.django_db
def test_unique_definition_and_version_is_enforced():
    definition = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")
    ActivityVersion.objects.create(definition=definition, version=1)

    with pytest.raises(IntegrityError):
        ActivityVersion.objects.create(definition=definition, version=1)


@pytest.mark.django_db
def test_different_definitions_may_each_have_their_own_version_one():
    first = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")
    second = ActivityDefinition.objects.create(name="Quiz B", type_key="assessment")

    first_version = ActivityVersion.objects.create(definition=first, version=1)
    second_version = ActivityVersion.objects.create(definition=second, version=1)

    assert first_version.version == second_version.version == 1


@pytest.mark.django_db
def test_activity_version_stores_config_and_required_capabilities():
    definition = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")

    version = ActivityVersion.objects.create(
        definition=definition,
        version=1,
        config={"question_count": 5},
        required_capabilities=["shell", "filesystem"],
    )
    reloaded = ActivityVersion.objects.get(pk=version.pk)

    assert reloaded.config == {"question_count": 5}
    assert reloaded.required_capabilities == ["shell", "filesystem"]


@pytest.mark.django_db
def test_activity_version_config_default_does_not_share_mutable_state():
    definition = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")

    first = ActivityVersion.objects.create(definition=definition, version=1)
    first.config["mutated"] = True

    second_definition = ActivityDefinition.objects.create(name="Quiz B", type_key="assessment")
    second = ActivityVersion.objects.create(definition=second_definition, version=1)

    assert second.config == {}
    assert second.required_capabilities == []


@pytest.mark.django_db
def test_activity_version_is_published_reflects_published_at():
    definition = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")
    version = ActivityVersion.objects.create(definition=definition, version=1)

    assert version.is_published is False
