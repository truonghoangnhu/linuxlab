"""Task 12 DB-enforced invariants: package identity uniqueness, at most one
active package per source, per-package artifact uniqueness, and
immutability of staged semantic artifacts through supported ORM mutation
paths (instance save, QuerySet.update(), bulk_update())."""

import uuid
from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, transaction

from linuxlab.sync.models import (
    ACTIVE,
    ARCHIVED,
    STAGED,
    InstalledActivityVersion,
    InstalledPackage,
    InstalledPublication,
    InstalledQuestionGrading,
    InstalledQuestionVersion,
)


def _make_package(**overrides):
    fields = {
        "package_type": "learning_path",
        "source_public_id": uuid.uuid4(),
        "package_version": 1,
        "payload_sha256": "0" * 64,
        "schema_version": 1,
        "minimum_linuxlab_version": "0.1.0",
        "signature_algorithm": "ed25519",
        "manifest_created_at": datetime.now(UTC),
        "state": STAGED,
    }
    fields.update(overrides)
    return InstalledPackage.objects.create(**fields)


# --- package identity uniqueness -------------------------------------------


@pytest.mark.django_db
def test_unique_package_identity_enforced_at_db_level():
    source_public_id = uuid.uuid4()
    _make_package(source_public_id=source_public_id, package_version=1)

    with pytest.raises(IntegrityError), transaction.atomic():
        _make_package(source_public_id=source_public_id, package_version=1)


@pytest.mark.django_db
def test_different_version_of_same_source_is_allowed():
    source_public_id = uuid.uuid4()
    _make_package(source_public_id=source_public_id, package_version=1)
    _make_package(source_public_id=source_public_id, package_version=2)

    assert InstalledPackage.objects.filter(source_public_id=source_public_id).count() == 2


# --- one active package per source -----------------------------------------


@pytest.mark.django_db
def test_only_one_active_package_per_source_enforced_at_db_level():
    source_public_id = uuid.uuid4()
    _make_package(source_public_id=source_public_id, package_version=1, state=ACTIVE)

    with pytest.raises(IntegrityError), transaction.atomic():
        _make_package(source_public_id=source_public_id, package_version=2, state=ACTIVE)


@pytest.mark.django_db
def test_active_and_archived_of_the_same_source_coexist():
    source_public_id = uuid.uuid4()
    _make_package(source_public_id=source_public_id, package_version=1, state=ARCHIVED)
    _make_package(source_public_id=source_public_id, package_version=2, state=ACTIVE)

    assert InstalledPackage.objects.filter(source_public_id=source_public_id).count() == 2


@pytest.mark.django_db
def test_two_different_sources_may_each_have_their_own_active_package():
    _make_package(source_public_id=uuid.uuid4(), package_version=1, state=ACTIVE)
    _make_package(source_public_id=uuid.uuid4(), package_version=1, state=ACTIVE)

    assert InstalledPackage.objects.filter(state=ACTIVE).count() == 2


# --- artifact uniqueness inside a package -----------------------------------


@pytest.mark.django_db
def test_installed_activity_version_unique_per_package():
    package = _make_package()
    activity_public_id = uuid.uuid4()
    InstalledActivityVersion.objects.create(
        package=package,
        activity_public_id=activity_public_id,
        version=1,
        type_key="assessment",
        config={},
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        InstalledActivityVersion.objects.create(
            package=package,
            activity_public_id=activity_public_id,
            version=1,
            type_key="assessment",
            config={},
        )


@pytest.mark.django_db
def test_installed_question_version_unique_per_package():
    package = _make_package()
    question_public_id = uuid.uuid4()
    InstalledQuestionVersion.objects.create(
        package=package,
        question_public_id=question_public_id,
        version=1,
        type_key="single_choice",
        content={},
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        InstalledQuestionVersion.objects.create(
            package=package,
            question_public_id=question_public_id,
            version=1,
            type_key="single_choice",
            content={},
        )


@pytest.mark.django_db
def test_installed_question_grading_is_one_to_one_with_its_question():
    package = _make_package()
    question = InstalledQuestionVersion.objects.create(
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={},
    )
    InstalledQuestionGrading.objects.create(
        question=question, answer_key={"correct_option_id": "a"}, scoring={"max_score": 1}
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        InstalledQuestionGrading.objects.create(
            question=question, answer_key={"correct_option_id": "b"}, scoring={"max_score": 1}
        )


@pytest.mark.django_db
def test_installed_publication_is_one_to_one_with_its_package():
    package = _make_package()
    InstalledPublication.objects.create(
        package=package,
        kind="learning_path",
        source_public_id=package.source_public_id,
        version=1,
        content_hash="0" * 64,
        payload={},
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        InstalledPublication.objects.create(
            package=package,
            kind="learning_path",
            source_public_id=package.source_public_id,
            version=1,
            content_hash="0" * 64,
            payload={},
        )


# --- staged artifact immutability -------------------------------------------


@pytest.mark.django_db
def test_installed_publication_instance_mutation_rejected():
    package = _make_package()
    publication = InstalledPublication.objects.create(
        package=package,
        kind="learning_path",
        source_public_id=package.source_public_id,
        version=1,
        content_hash="0" * 64,
        payload={"original": True},
    )

    publication.payload = {"tampered": True}
    with pytest.raises(ValueError, match="immutable"):
        publication.save()

    reloaded = InstalledPublication.objects.get(pk=publication.pk)
    assert reloaded.payload == {"original": True}


@pytest.mark.django_db
def test_installed_publication_queryset_update_rejected():
    package = _make_package()
    InstalledPublication.objects.create(
        package=package,
        kind="learning_path",
        source_public_id=package.source_public_id,
        version=1,
        content_hash="0" * 64,
        payload={"original": True},
    )

    with pytest.raises(ValueError, match="immutable"):
        InstalledPublication.objects.filter(package=package).update(payload={"tampered": True})


@pytest.mark.django_db
def test_installed_activity_version_instance_mutation_rejected():
    package = _make_package()
    activity = InstalledActivityVersion.objects.create(
        package=package,
        activity_public_id=uuid.uuid4(),
        version=1,
        type_key="assessment",
        config={"original": True},
    )

    activity.config = {"tampered": True}
    with pytest.raises(ValueError, match="immutable"):
        activity.save()


@pytest.mark.django_db
def test_installed_activity_version_queryset_update_rejected():
    package = _make_package()
    InstalledActivityVersion.objects.create(
        package=package,
        activity_public_id=uuid.uuid4(),
        version=1,
        type_key="assessment",
        config={},
    )

    with pytest.raises(ValueError, match="immutable"):
        InstalledActivityVersion.objects.filter(package=package).update(type_key="tampered")


@pytest.mark.django_db
def test_installed_question_version_instance_mutation_rejected():
    package = _make_package()
    question = InstalledQuestionVersion.objects.create(
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={"original": True},
    )

    question.content = {"tampered": True}
    with pytest.raises(ValueError, match="immutable"):
        question.save()


@pytest.mark.django_db
def test_installed_question_grading_instance_mutation_rejected():
    package = _make_package()
    question = InstalledQuestionVersion.objects.create(
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={},
    )
    grading = InstalledQuestionGrading.objects.create(
        question=question,
        answer_key={"correct_option_id": "a"},
        scoring={"max_score": 1},
    )

    grading.answer_key = {"correct_option_id": "z"}
    with pytest.raises(ValueError, match="immutable"):
        grading.save()


@pytest.mark.django_db
def test_installed_question_grading_queryset_update_rejected():
    package = _make_package()
    question = InstalledQuestionVersion.objects.create(
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={},
    )
    InstalledQuestionGrading.objects.create(
        question=question,
        answer_key={"correct_option_id": "a"},
        scoring={"max_score": 1},
    )

    with pytest.raises(ValueError, match="immutable"):
        InstalledQuestionGrading.objects.filter(question=question).update(
            answer_key={"correct_option_id": "z"}
        )


@pytest.mark.django_db
def test_installed_package_lifecycle_fields_remain_ordinarily_mutable():
    # Lifecycle metadata (state/activated_at/previous_package) is NOT
    # guarded the same way -- it changes only through
    # linuxlab.sync.services, but the model itself allows it (Task 12 §65).
    package = _make_package()
    package.state = ACTIVE
    package.save(update_fields=["state"])

    assert InstalledPackage.objects.get(pk=package.pk).state == ACTIVE
