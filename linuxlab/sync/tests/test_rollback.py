"""Task 12 rollback_package(): rollback to a recorded previous package
without rebuilding/re-verifying/redownloading, atomicity, and fail-closed
behavior when there is nothing to roll back to."""

from unittest.mock import patch

import pytest
from django.test import override_settings

from linuxlab.publication.package import build_package
from linuxlab.sync.models import ACTIVE, ARCHIVED, InstalledPackage
from linuxlab.sync.services import (
    PackageRollbackError,
    activate_package,
    import_package,
    rollback_package,
)

from .conftest import republish

pytestmark = pytest.mark.django_db(transaction=True)


def _stage(world, private_key, public_key):
    package_bytes = build_package(world["snapshot"], world["activity_registry"], private_key)
    return import_package(package_bytes, world["activity_registry"], public_key)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_rollback_restores_the_previous_active_package(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    activate_package(first)

    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )
    activate_package(second)

    rolled_back_to = rollback_package(first.package_type, first.source_public_id)

    assert rolled_back_to.pk == first.pk
    assert InstalledPackage.objects.get(pk=first.pk).state == ACTIVE
    assert InstalledPackage.objects.get(pk=second.pk).state == ARCHIVED


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_rollback_preserves_original_artifact_rows_unchanged(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    activate_package(first)
    original_config = first.activities.get().config
    original_content = first.questions.get().content
    original_answer_key = first.questions.get().grading.answer_key

    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )
    activate_package(second)

    rollback_package(first.package_type, first.source_public_id)

    first.refresh_from_db()
    assert first.activities.get().config == original_config
    assert first.questions.get().content == original_content
    assert first.questions.get().grading.answer_key == original_answer_key


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_rollback_without_a_previous_package_raises_and_leaves_state_untouched(
    quiz_world, keypair
):
    private_key, public_key = keypair
    package = _stage(quiz_world, private_key, public_key)
    activate_package(package)

    with pytest.raises(PackageRollbackError):
        rollback_package(package.package_type, package.source_public_id)

    assert InstalledPackage.objects.get(pk=package.pk).state == ACTIVE


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_rollback_with_no_active_package_raises():
    import uuid

    with pytest.raises(PackageRollbackError):
        rollback_package("learning_path", uuid.uuid4())


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_rollback_never_rebuilds_or_reverifies(quiz_world, keypair):
    """Rollback must never call build_package()/verify_package() or touch
    Central/network -- it only flips lifecycle state on already-staged
    local rows (Task 12 §29, §70, §73)."""
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    activate_package(first)

    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )
    activate_package(second)

    with (
        patch(
            "linuxlab.sync.services.verify_package",
            side_effect=AssertionError("rollback_package must never call verify_package"),
        ),
    ):
        rollback_package(first.package_type, first.source_public_id)

    assert InstalledPackage.objects.get(pk=first.pk).state == ACTIVE


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_failed_rollback_leaves_no_partial_state(quiz_world, keypair):
    """Force a failure after the current-active row has already been
    archived by the raw `.update()` call inside rollback_package()'s
    transaction: the whole transaction must roll back, leaving the
    pre-rollback state exactly as it was -- never zero-active, never
    two-active (Task 12 §32, §69)."""
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    activate_package(first)

    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )
    activate_package(second)

    with (
        patch(
            "linuxlab.sync.models.InstalledPackage.save",
            side_effect=RuntimeError("simulated failure"),
        ),
        pytest.raises(RuntimeError),
    ):
        rollback_package(first.package_type, first.source_public_id)

    assert InstalledPackage.objects.get(pk=second.pk).state == ACTIVE
    assert InstalledPackage.objects.get(pk=first.pk).state == ARCHIVED
    assert (
        InstalledPackage.objects.filter(
            package_type=first.package_type,
            source_public_id=first.source_public_id,
            state=ACTIVE,
        ).count()
        == 1
    )


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_rollback_rejects_a_previous_package_from_a_foreign_source(quiz_world, keypair):
    """Defense in depth: even if InstalledPackage.previous_package somehow
    pointed at a package outside this exact source scope, rollback fails
    closed rather than activating a foreign package."""
    private_key, public_key = keypair

    from .conftest import build_quiz_world

    other_world = build_quiz_world()
    other_package = _stage(other_world, private_key, public_key)

    package = _stage(quiz_world, private_key, public_key)
    activate_package(package)

    # Force an inconsistent linkage directly at the DB level (never a
    # supported lifecycle-service outcome) to prove rollback fails closed.
    InstalledPackage.objects.filter(pk=package.pk).update(previous_package=other_package.pk)

    with pytest.raises(PackageRollbackError):
        rollback_package(package.package_type, package.source_public_id)
