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
    archived (the first _write_installed_package_lifecycle() call inside
    rollback_package()'s transaction) but before the previous package's
    own reactivation commits: the whole transaction must roll back,
    leaving the pre-rollback state exactly as it was -- never zero-active,
    never two-active (Task 12 §32, §69)."""
    from linuxlab.sync import services as sync_services

    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    activate_package(first)

    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )
    activate_package(second)

    original = sync_services._write_installed_package_lifecycle
    calls = {"n": 0}

    def fail_on_second_write(pk, **fields):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure")
        return original(pk, **fields)

    with (
        patch(
            "linuxlab.sync.services._write_installed_package_lifecycle",
            side_effect=fail_on_second_write,
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
    closed rather than activating a foreign package. The corrupted linkage
    is forced via raw SQL (never a supported ORM/service write path,
    exactly like Task 11's own _corrupt_stored_content_hash() adversarial
    test technique) since ordinary QuerySet.update() is now blocked."""
    from django.db import connection

    private_key, public_key = keypair

    from .conftest import build_quiz_world

    other_world = build_quiz_world()
    other_package = _stage(other_world, private_key, public_key)

    package = _stage(quiz_world, private_key, public_key)
    activate_package(package)

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE sync_installedpackage SET previous_package_id = %s WHERE id = %s",
            [other_package.pk, package.pk],
        )

    with pytest.raises(PackageRollbackError):
        rollback_package(package.package_type, package.source_public_id)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_archived_package_cannot_be_deleted_and_rollback_still_works(quiz_world, keypair):
    """Task 12 corrective §29: an ARCHIVED package retains full rollback
    capability even though it cannot be deleted through ordinary ORM."""
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    activate_package(first)

    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )
    activate_package(second)

    first_reloaded = InstalledPackage.objects.get(pk=first.pk)
    assert first_reloaded.state == ARCHIVED

    with pytest.raises(ValueError, match="cannot be deleted"):
        first_reloaded.delete()
    with pytest.raises(ValueError, match="cannot be deleted"):
        InstalledPackage.objects.filter(pk=first.pk).delete()

    rollback_package(first.package_type, first.source_public_id)

    assert InstalledPackage.objects.get(pk=first.pk).state == ACTIVE
    assert InstalledPackage.objects.get(pk=second.pk).state == ARCHIVED


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_multi_version_rollback_chain(quiz_world, keypair):
    """Sanity test for historical linkage across three versions: v1 -> v2
    -> v3, then rollback twice, each time following its own genuine
    predecessor rather than a manufactured reciprocal link (Task 12
    corrective §34-35)."""
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    v1 = _stage(quiz_world, private_key, public_key)
    activate_package(v1)

    v2_snapshot = republish(quiz_world)
    v2 = import_package(build_package(v2_snapshot, registry, private_key), registry, public_key)
    activate_package(v2)

    v3_snapshot = republish(quiz_world)
    v3 = import_package(build_package(v3_snapshot, registry, private_key), registry, public_key)
    activate_package(v3)

    assert InstalledPackage.objects.get(pk=v1.pk).state == ARCHIVED
    v2_reloaded = InstalledPackage.objects.get(pk=v2.pk)
    assert v2_reloaded.state == ARCHIVED
    assert v2_reloaded.previous_package_id == v1.pk
    v3_reloaded = InstalledPackage.objects.get(pk=v3.pk)
    assert v3_reloaded.state == ACTIVE
    assert v3_reloaded.previous_package_id == v2.pk

    rollback_package(v1.package_type, v1.source_public_id)
    assert InstalledPackage.objects.get(pk=v2.pk).state == ACTIVE
    assert InstalledPackage.objects.get(pk=v3.pk).state == ARCHIVED

    rollback_package(v1.package_type, v1.source_public_id)
    assert InstalledPackage.objects.get(pk=v1.pk).state == ACTIVE
    assert InstalledPackage.objects.get(pk=v2.pk).state == ARCHIVED
