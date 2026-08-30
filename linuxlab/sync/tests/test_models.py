"""Task 12 DB-enforced invariants and the sealed trust boundary (corrective
review): package identity uniqueness, at most one active package per
source, per-package artifact uniqueness, a PostgreSQL CheckConstraint on
InstalledPackage.state, and proof that NO supported ORM API (create,
instance save, bulk_create, QuerySet.update()/bulk_update(), instance
delete, QuerySet.delete()) can fabricate, mutate, or destroy an
InstalledPackage or any staged semantic artifact.

`_make_package()`/`_insert_trusted` below use the same private trusted
insertion helper linuxlab.sync.services uses internally -- a legitimate
test-only use of an internal seam, exactly like Task 09's/Task 11's own
tests reach into private helpers to simulate a already-persisted row
without going through the full real import pipeline every time. It is
never a publicly exported API."""

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
from linuxlab.sync.services import _insert_trusted

pytestmark = pytest.mark.django_db


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
    return _insert_trusted(InstalledPackage, **fields)


# --- package identity uniqueness (DB-enforced) ------------------------------


def test_unique_package_identity_enforced_at_db_level():
    source_public_id = uuid.uuid4()
    _make_package(source_public_id=source_public_id, package_version=1)

    with pytest.raises(IntegrityError), transaction.atomic():
        _make_package(source_public_id=source_public_id, package_version=1)


def test_different_version_of_same_source_is_allowed():
    source_public_id = uuid.uuid4()
    _make_package(source_public_id=source_public_id, package_version=1)
    _make_package(source_public_id=source_public_id, package_version=2)

    assert InstalledPackage.objects.filter(source_public_id=source_public_id).count() == 2


# --- one active package per source (DB-enforced) ----------------------------


def test_only_one_active_package_per_source_enforced_at_db_level():
    source_public_id = uuid.uuid4()
    _make_package(source_public_id=source_public_id, package_version=1, state=ACTIVE)

    with pytest.raises(IntegrityError), transaction.atomic():
        _make_package(source_public_id=source_public_id, package_version=2, state=ACTIVE)


def test_active_and_archived_of_the_same_source_coexist():
    source_public_id = uuid.uuid4()
    _make_package(source_public_id=source_public_id, package_version=1, state=ARCHIVED)
    _make_package(source_public_id=source_public_id, package_version=2, state=ACTIVE)

    assert InstalledPackage.objects.filter(source_public_id=source_public_id).count() == 2


def test_two_different_sources_may_each_have_their_own_active_package():
    _make_package(source_public_id=uuid.uuid4(), package_version=1, state=ACTIVE)
    _make_package(source_public_id=uuid.uuid4(), package_version=1, state=ACTIVE)

    assert InstalledPackage.objects.filter(state=ACTIVE).count() == 2


# --- state CheckConstraint (DB-enforced) ------------------------------------


def test_invalid_state_value_rejected_by_db_check_constraint():
    with pytest.raises(IntegrityError), transaction.atomic():
        _make_package(state="whatever")


# --- artifact uniqueness inside a package (DB-enforced) ---------------------


def test_installed_activity_version_unique_per_package():
    package = _make_package()
    activity_public_id = uuid.uuid4()
    _insert_trusted(
        InstalledActivityVersion,
        package=package,
        activity_public_id=activity_public_id,
        version=1,
        type_key="assessment",
        config={},
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        _insert_trusted(
            InstalledActivityVersion,
            package=package,
            activity_public_id=activity_public_id,
            version=1,
            type_key="assessment",
            config={},
        )


def test_installed_question_version_unique_per_package():
    package = _make_package()
    question_public_id = uuid.uuid4()
    _insert_trusted(
        InstalledQuestionVersion,
        package=package,
        question_public_id=question_public_id,
        version=1,
        type_key="single_choice",
        content={},
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        _insert_trusted(
            InstalledQuestionVersion,
            package=package,
            question_public_id=question_public_id,
            version=1,
            type_key="single_choice",
            content={},
        )


def test_installed_question_grading_is_one_to_one_with_its_question():
    package = _make_package()
    question = _insert_trusted(
        InstalledQuestionVersion,
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={},
    )
    _insert_trusted(
        InstalledQuestionGrading,
        question=question,
        answer_key={"correct_option_id": "a"},
        scoring={"max_score": 1},
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        _insert_trusted(
            InstalledQuestionGrading,
            question=question,
            answer_key={"correct_option_id": "b"},
            scoring={"max_score": 1},
        )


def test_installed_publication_is_one_to_one_with_its_package():
    package = _make_package()
    _insert_trusted(
        InstalledPublication,
        package=package,
        kind="learning_path",
        source_public_id=package.source_public_id,
        version=1,
        content_hash="0" * 64,
        payload={},
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        _insert_trusted(
            InstalledPublication,
            package=package,
            kind="learning_path",
            source_public_id=package.source_public_id,
            version=1,
            content_hash="0" * 64,
            payload={},
        )


# --- InstalledPackage: public ORM fabrication is blocked --------------------


def test_ordinary_objects_create_cannot_fabricate_installed_package():
    with pytest.raises(ValueError, match="cannot be created"):
        InstalledPackage.objects.create(
            package_type="learning_path",
            source_public_id=uuid.uuid4(),
            package_version=1,
            payload_sha256="0" * 64,
            schema_version=1,
            minimum_linuxlab_version="0.1.0",
            signature_algorithm="ed25519",
            manifest_created_at=datetime.now(UTC),
        )
    assert InstalledPackage.objects.count() == 0


def test_ordinary_instance_save_cannot_fabricate_installed_package():
    package = InstalledPackage(
        package_type="learning_path",
        source_public_id=uuid.uuid4(),
        package_version=1,
        payload_sha256="0" * 64,
        schema_version=1,
        minimum_linuxlab_version="0.1.0",
        signature_algorithm="ed25519",
        manifest_created_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="cannot be created"):
        package.save()
    assert InstalledPackage.objects.count() == 0


def test_ordinary_bulk_create_cannot_fabricate_installed_package():
    package = InstalledPackage(
        package_type="learning_path",
        source_public_id=uuid.uuid4(),
        package_version=1,
        payload_sha256="0" * 64,
        schema_version=1,
        minimum_linuxlab_version="0.1.0",
        signature_algorithm="ed25519",
        manifest_created_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="bulk_create"):
        InstalledPackage.objects.bulk_create([package])
    assert InstalledPackage.objects.count() == 0


def test_default_manager_and_base_manager_both_reject_fabrication():
    """Guard against a live bypass through _default_manager/_base_manager
    resolving to a bare, unguarded models.Manager() (Task 09 lesson)."""
    fields = {
        "package_type": "learning_path",
        "source_public_id": uuid.uuid4(),
        "package_version": 1,
        "payload_sha256": "0" * 64,
        "schema_version": 1,
        "minimum_linuxlab_version": "0.1.0",
        "signature_algorithm": "ed25519",
        "manifest_created_at": datetime.now(UTC),
    }
    with pytest.raises(ValueError, match="cannot be created"):
        InstalledPackage._default_manager.create(**fields)
    with pytest.raises(ValueError, match="cannot be created"):
        InstalledPackage._base_manager.create(**fields)
    assert InstalledPackage.objects.count() == 0


# --- InstalledPackage: provenance immutability ------------------------------


def test_provenance_instance_mutation_rejected():
    package = _make_package()
    original_hash = package.payload_sha256

    package.payload_sha256 = "f" * 64
    with pytest.raises(ValueError, match="cannot be created or modified"):
        package.save()

    assert InstalledPackage.objects.get(pk=package.pk).payload_sha256 == original_hash


def test_provenance_queryset_update_rejected():
    package = _make_package()
    original_hash = package.payload_sha256

    with pytest.raises(ValueError, match="cannot be mutated"):
        InstalledPackage.objects.filter(pk=package.pk).update(payload_sha256="f" * 64)

    assert InstalledPackage.objects.get(pk=package.pk).payload_sha256 == original_hash


def test_ordinary_bulk_update_rejected():
    package = _make_package()
    package.payload_sha256 = "f" * 64
    with pytest.raises(ValueError, match="cannot be mutated"):
        InstalledPackage.objects.bulk_update([package], ["payload_sha256"])


def test_ordinary_save_cannot_change_state_as_a_substitute_for_activation():
    package = _make_package()
    package.state = ACTIVE
    with pytest.raises(ValueError, match="cannot be created or modified"):
        package.save()

    assert InstalledPackage.objects.get(pk=package.pk).state == STAGED


# --- InstalledPackage: deletion is blocked ----------------------------------


def test_installed_package_instance_delete_rejected():
    package = _make_package()
    with pytest.raises(ValueError, match="cannot be deleted"):
        package.delete()
    assert InstalledPackage.objects.filter(pk=package.pk).exists()


def test_installed_package_queryset_delete_rejected():
    package = _make_package()
    with pytest.raises(ValueError, match="cannot be deleted"):
        InstalledPackage.objects.filter(pk=package.pk).delete()
    assert InstalledPackage.objects.filter(pk=package.pk).exists()


# --- staged artifact fabrication is blocked ---------------------------------


def test_ordinary_objects_create_cannot_fabricate_installed_activity_version():
    package = _make_package()
    with pytest.raises(ValueError, match="cannot be created"):
        InstalledActivityVersion.objects.create(
            package=package,
            activity_public_id=uuid.uuid4(),
            version=1,
            type_key="assessment",
            config={},
        )
    assert InstalledActivityVersion.objects.count() == 0


def test_ordinary_bulk_create_cannot_fabricate_installed_question_version():
    package = _make_package()
    fabricated = InstalledQuestionVersion(
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={},
    )
    with pytest.raises(ValueError, match="bulk_create"):
        InstalledQuestionVersion.objects.bulk_create([fabricated])
    assert InstalledQuestionVersion.objects.count() == 0


def test_ordinary_instance_save_cannot_fabricate_installed_question_grading():
    package = _make_package()
    question = _insert_trusted(
        InstalledQuestionVersion,
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={},
    )
    grading = InstalledQuestionGrading(
        question=question, answer_key={"correct_option_id": "a"}, scoring={"max_score": 1}
    )
    with pytest.raises(ValueError, match="cannot be created"):
        grading.save()
    assert InstalledQuestionGrading.objects.count() == 0


# --- staged artifact immutability -------------------------------------------


def test_installed_publication_instance_mutation_rejected():
    package = _make_package()
    publication = _insert_trusted(
        InstalledPublication,
        package=package,
        kind="learning_path",
        source_public_id=package.source_public_id,
        version=1,
        content_hash="0" * 64,
        payload={"original": True},
    )

    publication.payload = {"tampered": True}
    with pytest.raises(ValueError, match="cannot be created or modified"):
        publication.save()

    reloaded = InstalledPublication.objects.get(pk=publication.pk)
    assert reloaded.payload == {"original": True}


def test_installed_publication_queryset_update_rejected():
    package = _make_package()
    _insert_trusted(
        InstalledPublication,
        package=package,
        kind="learning_path",
        source_public_id=package.source_public_id,
        version=1,
        content_hash="0" * 64,
        payload={"original": True},
    )

    with pytest.raises(ValueError, match="immutable"):
        InstalledPublication.objects.filter(package=package).update(payload={"tampered": True})


def test_installed_activity_version_instance_mutation_rejected():
    package = _make_package()
    activity = _insert_trusted(
        InstalledActivityVersion,
        package=package,
        activity_public_id=uuid.uuid4(),
        version=1,
        type_key="assessment",
        config={"original": True},
    )

    activity.config = {"tampered": True}
    with pytest.raises(ValueError, match="cannot be created or modified"):
        activity.save()


def test_installed_activity_version_queryset_update_rejected():
    package = _make_package()
    _insert_trusted(
        InstalledActivityVersion,
        package=package,
        activity_public_id=uuid.uuid4(),
        version=1,
        type_key="assessment",
        config={},
    )

    with pytest.raises(ValueError, match="immutable"):
        InstalledActivityVersion.objects.filter(package=package).update(type_key="tampered")


def test_installed_activity_version_bulk_update_rejected():
    package = _make_package()
    activity = _insert_trusted(
        InstalledActivityVersion,
        package=package,
        activity_public_id=uuid.uuid4(),
        version=1,
        type_key="assessment",
        config={},
    )
    activity.type_key = "tampered"
    with pytest.raises(ValueError, match="immutable"):
        InstalledActivityVersion.objects.bulk_update([activity], ["type_key"])


def test_installed_question_version_instance_mutation_rejected():
    package = _make_package()
    question = _insert_trusted(
        InstalledQuestionVersion,
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={"original": True},
    )

    question.content = {"tampered": True}
    with pytest.raises(ValueError, match="cannot be created or modified"):
        question.save()


def test_installed_question_grading_instance_mutation_rejected():
    package = _make_package()
    question = _insert_trusted(
        InstalledQuestionVersion,
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={},
    )
    grading = _insert_trusted(
        InstalledQuestionGrading,
        question=question,
        answer_key={"correct_option_id": "a"},
        scoring={"max_score": 1},
    )

    grading.answer_key = {"correct_option_id": "z"}
    with pytest.raises(ValueError, match="cannot be created or modified"):
        grading.save()


def test_installed_question_grading_queryset_update_rejected():
    package = _make_package()
    question = _insert_trusted(
        InstalledQuestionVersion,
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={},
    )
    _insert_trusted(
        InstalledQuestionGrading,
        question=question,
        answer_key={"correct_option_id": "a"},
        scoring={"max_score": 1},
    )

    with pytest.raises(ValueError, match="immutable"):
        InstalledQuestionGrading.objects.filter(question=question).update(
            answer_key={"correct_option_id": "z"}
        )


# --- staged artifact deletion is blocked ------------------------------------


def test_installed_publication_instance_delete_rejected():
    package = _make_package()
    publication = _insert_trusted(
        InstalledPublication,
        package=package,
        kind="learning_path",
        source_public_id=package.source_public_id,
        version=1,
        content_hash="0" * 64,
        payload={},
    )
    with pytest.raises(ValueError, match="immutable"):
        publication.delete()
    assert InstalledPublication.objects.filter(pk=publication.pk).exists()


def test_installed_activity_version_queryset_delete_rejected():
    package = _make_package()
    _insert_trusted(
        InstalledActivityVersion,
        package=package,
        activity_public_id=uuid.uuid4(),
        version=1,
        type_key="assessment",
        config={},
    )
    with pytest.raises(ValueError, match="immutable"):
        InstalledActivityVersion.objects.filter(package=package).delete()
    assert InstalledActivityVersion.objects.filter(package=package).count() == 1


def test_installed_question_version_instance_delete_rejected():
    package = _make_package()
    question = _insert_trusted(
        InstalledQuestionVersion,
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={},
    )
    with pytest.raises(ValueError, match="immutable"):
        question.delete()
    assert InstalledQuestionVersion.objects.filter(pk=question.pk).exists()


def test_installed_question_grading_queryset_delete_rejected():
    package = _make_package()
    question = _insert_trusted(
        InstalledQuestionVersion,
        package=package,
        question_public_id=uuid.uuid4(),
        version=1,
        type_key="single_choice",
        content={},
    )
    _insert_trusted(
        InstalledQuestionGrading,
        question=question,
        answer_key={"correct_option_id": "a"},
        scoring={"max_score": 1},
    )
    with pytest.raises(ValueError, match="immutable"):
        InstalledQuestionGrading.objects.filter(question=question).delete()
    assert InstalledQuestionGrading.objects.filter(question=question).exists()


# --- reads remain unaffected -------------------------------------------------


def test_reads_remain_unaffected_by_the_guards():
    package = _make_package()
    _insert_trusted(
        InstalledPublication,
        package=package,
        kind="learning_path",
        source_public_id=package.source_public_id,
        version=1,
        content_hash="0" * 64,
        payload={},
    )

    assert InstalledPackage.objects.get(pk=package.pk).pk == package.pk
    assert InstalledPackage.objects.filter(pk=package.pk).exists()
    assert InstalledPackage.objects.filter(pk=package.pk).count() == 1
    assert list(InstalledPackage.objects.select_related("publication").filter(pk=package.pk))
    package.refresh_from_db()
    assert package.state == STAGED
