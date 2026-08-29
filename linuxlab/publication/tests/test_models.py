import uuid

import pytest
from django.db import IntegrityError

from linuxlab.publication.models import PublicationSnapshot
from linuxlab.publication.snapshots import _insert_published_snapshot


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


def _insert(**overrides):
    fields = {
        "kind": "learning_path",
        "source_public_id": uuid.uuid4(),
        "version": 1,
        "payload": {"a": 1},
        "content_hash": "0" * 64,
    }
    fields.update(overrides)
    return _insert_published_snapshot(**fields)


# --- Task 09 corrective: ordinary creation is rejected outright -----------
# (Critical 1: a PublicationSnapshot row must be trustworthy by
# construction; the ONLY legitimate writer is the publication service.)


@pytest.mark.django_db
def test_ordinary_save_of_an_otherwise_normal_snapshot_is_rejected():
    snapshot = _make_snapshot()

    with pytest.raises(ValueError):
        snapshot.save()

    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_ordinary_create_with_hash_payload_mismatch_is_rejected():
    with pytest.raises(ValueError):
        PublicationSnapshot.objects.create(
            kind="learning_path",
            source_public_id=uuid.uuid4(),
            version=1,
            payload={"a": 1},
            content_hash="f" * 64,  # does not match sha256({"a":1})
        )

    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_ordinary_create_with_answer_key_bearing_payload_is_rejected():
    with pytest.raises(ValueError):
        PublicationSnapshot.objects.create(
            kind="learning_path",
            source_public_id=uuid.uuid4(),
            version=1,
            payload={"answer_key": {"correct_option_id": "a"}},
            content_hash="0" * 64,
        )

    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_ordinary_create_with_arbitrary_version_gap_is_rejected():
    with pytest.raises(ValueError):
        PublicationSnapshot.objects.create(
            kind="learning_path",
            source_public_id=uuid.uuid4(),
            version=50,
            payload={"a": 1},
            content_hash="0" * 64,
        )

    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_ordinary_create_with_explicit_local_pk_is_rejected():
    with pytest.raises(ValueError):
        PublicationSnapshot.objects.create(
            pk=999999,
            kind="learning_path",
            source_public_id=uuid.uuid4(),
            version=1,
            payload={"a": 1},
            content_hash="0" * 64,
        )

    assert PublicationSnapshot.objects.filter(pk=999999).exists() is False
    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_get_or_create_cannot_fabricate_a_snapshot():
    with pytest.raises(ValueError):
        PublicationSnapshot.objects.get_or_create(
            kind="learning_path",
            source_public_id=uuid.uuid4(),
            version=1,
            defaults={"payload": {"a": 1}, "content_hash": "0" * 64},
        )

    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_bulk_create_cannot_fabricate_snapshots():
    with pytest.raises(ValueError):
        PublicationSnapshot.objects.bulk_create([_make_snapshot()])

    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_queryset_update_cannot_mutate_an_existing_snapshot():
    original = _insert()

    with pytest.raises(ValueError):
        PublicationSnapshot.objects.filter(pk=original.pk).update(payload={"tampered": True})

    reloaded = PublicationSnapshot.objects.get(pk=original.pk)
    assert reloaded.payload == {"a": 1}


# --- controlled internal insertion still works as designed ----------------


@pytest.mark.django_db
def test_controlled_internal_insertion_creates_a_normal_orm_readable_row():
    snapshot = _insert()

    assert snapshot.pk is not None
    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    assert reloaded.version == 1
    assert reloaded.content_hash == "0" * 64


@pytest.mark.django_db
def test_controlled_internal_insertion_enforces_unique_kind_source_version():
    source_public_id = uuid.uuid4()
    _insert(source_public_id=source_public_id, version=1)

    with pytest.raises(IntegrityError):
        _insert(source_public_id=source_public_id, version=1)


@pytest.mark.django_db
def test_controlled_internal_insertion_still_hits_the_version_gte_1_check_constraint():
    from django.db import transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        _insert(version=0)


# --- immutability after creation -------------------------------------------


@pytest.mark.django_db
def test_snapshot_cannot_be_mutated_through_normal_save():
    snapshot = _insert()

    snapshot.payload = {"tampered": True}
    with pytest.raises(ValueError):
        snapshot.save()

    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    assert reloaded.payload == {"a": 1}


@pytest.mark.django_db
def test_snapshot_hash_cannot_be_mutated_through_normal_save():
    snapshot = _insert()

    snapshot.content_hash = "1" * 64
    with pytest.raises(ValueError):
        snapshot.save()

    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    assert reloaded.content_hash == "0" * 64


@pytest.mark.django_db
def test_snapshot_version_cannot_be_mutated_through_normal_save():
    snapshot = _insert()

    snapshot.version = 999
    with pytest.raises(ValueError):
        snapshot.save()

    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    assert reloaded.version == 1


@pytest.mark.django_db
def test_snapshot_source_public_id_cannot_be_mutated_through_normal_save():
    snapshot = _insert()
    other_source = uuid.uuid4()

    snapshot.source_public_id = other_source
    with pytest.raises(ValueError):
        snapshot.save()

    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    assert reloaded.source_public_id != other_source


# --- explicit local PK / resurrection (Task 05-07 lesson) ------------------


@pytest.mark.django_db
def test_explicit_unused_pk_creation_is_rejected():
    from django.db.models import Max

    unused_pk = (PublicationSnapshot.objects.aggregate(Max("pk"))["pk__max"] or 0) + 1000
    snapshot = _make_snapshot()
    snapshot.pk = unused_pk

    assert snapshot._state.adding is True

    with pytest.raises(ValueError):
        snapshot.save()

    assert PublicationSnapshot.objects.filter(pk=unused_pk).exists() is False


@pytest.mark.django_db
def test_explicit_existing_pk_cannot_overwrite_an_existing_snapshot():
    original = _insert()

    impersonator = _make_snapshot(source_public_id=uuid.uuid4(), payload={"hijacked": True})
    impersonator.pk = original.pk
    assert impersonator._state.adding is True

    with pytest.raises(ValueError):
        impersonator.save()

    reloaded = PublicationSnapshot.objects.get(pk=original.pk)
    assert reloaded.payload == {"a": 1}


@pytest.mark.django_db
def test_save_on_a_deleted_snapshot_is_rejected_not_resurrected():
    snapshot = _insert()
    stale = PublicationSnapshot.objects.get(pk=snapshot.pk)

    PublicationSnapshot.objects.filter(pk=snapshot.pk).delete()

    with pytest.raises(ValueError):
        stale.save()

    assert PublicationSnapshot.objects.filter(pk=snapshot.pk).exists() is False
