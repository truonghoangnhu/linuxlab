import uuid

import pytest
from django.apps import apps
from django.db import IntegrityError, transaction

from linuxlab.core.events import publish_event
from linuxlab.core.models import DomainEvent, StableIdentityModel


def test_stable_identity_model_is_abstract():
    assert StableIdentityModel._meta.abstract is True


def test_stable_identity_model_public_id_field_properties():
    field = StableIdentityModel._meta.get_field("public_id")
    assert field.get_internal_type() == "UUIDField"
    assert field.unique is True
    assert field.editable is False
    assert callable(field.default)
    assert field.default is uuid.uuid4


def test_stable_identity_model_creates_no_table():
    # Abstract models are never added to the app registry's concrete model
    # list, so this proves no table was created without touching the DB.
    assert StableIdentityModel not in apps.get_models()


@pytest.mark.django_db
def test_publish_event_creates_distinct_uuid_event_ids():
    first = publish_event("activity.completed", {"score": 1.0}, uuid.uuid4())
    second = publish_event("activity.completed", {"score": 1.0}, uuid.uuid4())

    assert isinstance(first.event_id, uuid.UUID)
    assert isinstance(second.event_id, uuid.UUID)
    assert first.event_id != second.event_id
    assert DomainEvent.objects.filter(pk=first.pk).exists()
    assert DomainEvent.objects.filter(pk=second.pk).exists()


@pytest.mark.django_db
def test_publish_event_preserves_event_type_payload_and_aggregate_id():
    aggregate_id = uuid.uuid4()
    payload = {"attempt_public_id": "example", "score": 1.0}

    event = publish_event("activity.completed", payload, aggregate_id)
    stored = DomainEvent.objects.get(pk=event.pk)

    assert stored.event_type == "activity.completed"
    assert stored.payload == payload
    assert stored.aggregate_id == aggregate_id


@pytest.mark.django_db
def test_domain_event_timestamps_on_creation():
    event = publish_event("lesson.completed", {}, uuid.uuid4())
    stored = DomainEvent.objects.get(pk=event.pk)

    assert stored.created_at is not None
    assert stored.processed_at is None


@pytest.mark.django_db
def test_domain_event_id_is_unique_at_database_level():
    duplicate_id = uuid.uuid4()
    DomainEvent.objects.create(
        event_id=duplicate_id, event_type="activity.completed", payload={}
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        DomainEvent.objects.create(
            event_id=duplicate_id, event_type="activity.completed", payload={}
        )
