import uuid

import pytest
from django.db import IntegrityError

from linuxlab.publication.models import PublicationSnapshot


def _make_snapshot(**overrides):
    fields = {
        "kind": "learning_path",
        "source_public_id": uuid.uuid4(),
        "version": 1,
        "payload": {"a": 1},
        "content_hash": "0" * 64,
    }
    fields.update(overrides)
    return PublicationSnapshot(**fields)


# --- creation ---------------------------------------------------------


@pytest.mark.django_db
def test_first_snapshot_can_be_created_directly():
    snapshot = _make_snapshot()
    snapshot.save()

    assert snapshot.version == 1
    assert snapshot.content_hash == "0" * 64


@pytest.mark.django_db
def test_unique_kind_source_version_is_enforced():
    source_public_id = uuid.uuid4()
    _make_snapshot(source_public_id=source_public_id, version=1).save()

    with pytest.raises(IntegrityError):
        _make_snapshot(source_public_id=source_public_id, version=1).save()


@pytest.mark.django_db
def test_version_zero_is_rejected_by_the_database_constraint():
    from django.db import transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        _make_snapshot(version=0).save()


# --- immutability -------------------------------------------------------


@pytest.mark.django_db
def test_snapshot_cannot_be_mutated_through_normal_save():
    snapshot = _make_snapshot()
    snapshot.save()

    snapshot.payload = {"tampered": True}
    with pytest.raises(ValueError):
        snapshot.save()

    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    assert reloaded.payload == {"a": 1}


@pytest.mark.django_db
def test_snapshot_hash_cannot_be_mutated_through_normal_save():
    snapshot = _make_snapshot()
    snapshot.save()

    snapshot.content_hash = "1" * 64
    with pytest.raises(ValueError):
        snapshot.save()

    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    assert reloaded.content_hash == "0" * 64


@pytest.mark.django_db
def test_snapshot_version_cannot_be_mutated_through_normal_save():
    snapshot = _make_snapshot()
    snapshot.save()

    snapshot.version = 999
    with pytest.raises(ValueError):
        snapshot.save()

    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    assert reloaded.version == 1


@pytest.mark.django_db
def test_snapshot_source_public_id_cannot_be_mutated_through_normal_save():
    snapshot = _make_snapshot()
    snapshot.save()
    other_source = uuid.uuid4()

    snapshot.source_public_id = other_source
    with pytest.raises(ValueError):
        snapshot.save()

    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    assert reloaded.source_public_id != other_source


# --- explicit local PK (Task 05-07 lesson) -------------------------------


def _unused_snapshot_pk():
    from django.db.models import Max

    current_max = PublicationSnapshot.objects.aggregate(Max("pk"))["pk__max"] or 0
    return current_max + 1000


@pytest.mark.django_db
def test_explicit_unused_pk_creation_is_rejected():
    unused_pk = _unused_snapshot_pk()
    snapshot = _make_snapshot()
    snapshot.pk = unused_pk

    assert snapshot._state.adding is True
    assert snapshot.pk == unused_pk

    with pytest.raises(ValueError):
        snapshot.save()

    assert PublicationSnapshot.objects.filter(pk=unused_pk).exists() is False


@pytest.mark.django_db
def test_explicit_existing_pk_cannot_overwrite_an_existing_snapshot():
    original = _make_snapshot()
    original.save()

    impersonator = _make_snapshot(source_public_id=uuid.uuid4(), payload={"hijacked": True})
    impersonator.pk = original.pk
    assert impersonator._state.adding is True

    with pytest.raises(ValueError):
        impersonator.save()

    reloaded = PublicationSnapshot.objects.get(pk=original.pk)
    assert reloaded.payload == {"a": 1}


@pytest.mark.django_db
def test_save_on_a_deleted_snapshot_is_rejected_not_resurrected():
    snapshot = _make_snapshot()
    snapshot.save()
    stale = PublicationSnapshot.objects.get(pk=snapshot.pk)

    PublicationSnapshot.objects.filter(pk=snapshot.pk).delete()

    with pytest.raises(ValueError):
        stale.save()

    assert PublicationSnapshot.objects.filter(pk=snapshot.pk).exists() is False
