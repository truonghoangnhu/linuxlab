import uuid
from typing import ClassVar

import pytest
from django import forms
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

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


# --- type_key is immutable after creation (Task 06 corrective) -----------


@pytest.mark.django_db
def test_type_key_change_is_rejected_after_creation():
    definition = ActivityDefinition.objects.create(name="Shell Quiz", type_key="assessment")

    definition.type_key = "terminal"
    with pytest.raises(ValueError):
        definition.save()

    reloaded = ActivityDefinition.objects.get(pk=definition.pk)
    assert reloaded.type_key == "assessment"


@pytest.mark.django_db
def test_name_remains_editable_after_creation():
    definition = ActivityDefinition.objects.create(name="Shell Quiz", type_key="assessment")

    definition.name = "Shell Quiz v2"
    definition.save()

    reloaded = ActivityDefinition.objects.get(pk=definition.pk)
    assert reloaded.name == "Shell Quiz v2"
    assert reloaded.type_key == "assessment"


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


# --- version >= 1 -----------------------------------------------------


@pytest.mark.django_db
def test_version_zero_is_rejected_by_model_validation():
    definition = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")
    version = ActivityVersion(
        definition=definition, version=0, config={"a": 1}, required_capabilities=["shell"]
    )

    with pytest.raises(ValidationError):
        version.full_clean()


@pytest.mark.django_db
def test_version_zero_is_rejected_by_the_database_constraint():
    definition = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")

    with pytest.raises(IntegrityError), transaction.atomic():
        ActivityVersion.objects.create(definition=definition, version=0)

    # the surrounding test transaction must still be usable afterward
    assert ActivityVersion.objects.filter(definition=definition).count() == 0


@pytest.mark.django_db
def test_version_one_remains_valid():
    definition = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")
    version = ActivityVersion(
        definition=definition, version=1, config={"a": 1}, required_capabilities=["shell"]
    )

    version.full_clean()
    version.save()

    assert version.version == 1


# --- empty config/required_capabilities are valid domain states ----------


@pytest.mark.django_db
def test_default_config_and_capabilities_pass_full_clean():
    definition = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")
    version = ActivityVersion(definition=definition, version=1)

    version.full_clean()

    assert version.config == {}
    assert version.required_capabilities == []


@pytest.mark.django_db
def test_default_config_and_capabilities_are_preserved_through_save_and_reload():
    definition = ActivityDefinition.objects.create(name="Quiz A", type_key="assessment")
    version = ActivityVersion(definition=definition, version=1)

    version.full_clean()
    version.save()

    reloaded = ActivityVersion.objects.get(pk=version.pk)
    assert reloaded.config == {}
    assert reloaded.required_capabilities == []


class _ActivityVersionConfigForm(forms.ModelForm):
    class Meta:
        model = ActivityVersion
        fields: ClassVar[list[str]] = ["config", "required_capabilities"]


def test_config_and_required_capabilities_are_not_required_on_modelform():
    form = _ActivityVersionConfigForm()

    assert form.fields["config"].required is False
    assert form.fields["required_capabilities"].required is False


def test_modelform_accepts_empty_json_values():
    form = _ActivityVersionConfigForm(data={"config": "{}", "required_capabilities": "[]"})

    assert form.is_valid(), form.errors
    assert form.cleaned_data["config"] == {}
    assert form.cleaned_data["required_capabilities"] == []
