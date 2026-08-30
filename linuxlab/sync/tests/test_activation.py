"""Task 12 activate_package(): atomic activation, one active package per
source scope, cross-source isolation, idempotent re-activation, and
invalid-state rejection."""

from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from linuxlab.publication.package import build_package
from linuxlab.sync.models import ACTIVE, ARCHIVED, STAGED, InstalledPackage
from linuxlab.sync.services import (
    PackageActivationError,
    activate_package,
    get_active_package,
    import_package,
)

from .conftest import build_quiz_world, republish

pytestmark = pytest.mark.django_db(transaction=True)


def _stage(world, private_key, public_key):
    package_bytes = build_package(world["snapshot"], world["activity_registry"], private_key)
    return import_package(package_bytes, world["activity_registry"], public_key)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activating_a_staged_package_makes_it_active(quiz_world, keypair):
    private_key, public_key = keypair
    package = _stage(quiz_world, private_key, public_key)

    activated = activate_package(package)

    assert activated.state == ACTIVE
    assert activated.activated_at is not None
    assert activated.previous_package is None
    assert get_active_package(package.package_type, package.source_public_id).pk == package.pk


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activating_a_second_version_archives_the_first(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    activate_package(first)

    second_snapshot = republish(quiz_world)
    second_bytes = build_package(second_snapshot, registry, private_key)
    second = import_package(second_bytes, registry, public_key)
    assert second.state == STAGED  # staging alone never activates

    activate_package(second)

    reloaded_first = InstalledPackage.objects.get(pk=first.pk)
    reloaded_second = InstalledPackage.objects.get(pk=second.pk)
    assert reloaded_first.state == ARCHIVED
    assert reloaded_second.state == ACTIVE
    assert reloaded_second.previous_package_id == reloaded_first.pk


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activating_an_already_active_package_is_idempotent_noop(quiz_world, keypair):
    private_key, public_key = keypair
    package = _stage(quiz_world, private_key, public_key)
    activate_package(package)
    first_activated_at = InstalledPackage.objects.get(pk=package.pk).activated_at

    result = activate_package(package)

    reloaded = InstalledPackage.objects.get(pk=package.pk)
    assert result.state == ACTIVE
    assert reloaded.activated_at == first_activated_at
    assert InstalledPackage.objects.filter(state=ACTIVE).count() == 1


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activating_a_nonexistent_package_raises():
    fake = InstalledPackage(pk=999999)

    with pytest.raises(PackageActivationError):
        activate_package(fake)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_only_one_active_package_per_source_after_activation(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    activate_package(first)

    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )
    activate_package(second)

    active_count = InstalledPackage.objects.filter(
        package_type=first.package_type,
        source_public_id=first.source_public_id,
        state=ACTIVE,
    ).count()
    assert active_count == 1


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activating_source_a_does_not_affect_source_b(keypair):
    private_key, public_key = keypair

    world_a = build_quiz_world()
    world_b = build_quiz_world()

    package_a = _stage(world_a, private_key, public_key)
    package_b = _stage(world_b, private_key, public_key)

    activate_package(package_a)
    activate_package(package_b)

    assert InstalledPackage.objects.get(pk=package_a.pk).state == ACTIVE
    assert InstalledPackage.objects.get(pk=package_b.pk).state == ACTIVE

    # Activating a new version of A must not disturb B.
    second_snapshot_a = republish(world_a)
    package_a2 = import_package(
        build_package(second_snapshot_a, world_a["activity_registry"], private_key),
        world_a["activity_registry"],
        public_key,
    )
    activate_package(package_a2)

    assert InstalledPackage.objects.get(pk=package_a.pk).state == ARCHIVED
    assert InstalledPackage.objects.get(pk=package_a2.pk).state == ACTIVE
    assert InstalledPackage.objects.get(pk=package_b.pk).state == ACTIVE


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activation_does_not_modify_semantic_artifact_rows(quiz_world, keypair):
    private_key, public_key = keypair
    package = _stage(quiz_world, private_key, public_key)

    before_config = package.activities.get().config
    before_content = package.questions.get().content
    before_grading = package.questions.get().grading.answer_key

    activate_package(package)

    package.refresh_from_db()
    assert package.activities.get().config == before_config
    assert package.questions.get().content == before_content
    assert package.questions.get().grading.answer_key == before_grading


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_concurrent_activation_never_leaves_two_active_packages(quiz_world, keypair):
    """DB-level invariant test: even bypassing activate_package()'s own
    scope-locking by writing lifecycle state directly through the private
    helper (simulating a hypothetical second, differently-coded writer),
    the partial unique index is the final backstop -- at most one row can
    ever be committed ACTIVE for one source."""
    from linuxlab.sync.services import _write_installed_package_lifecycle

    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )

    activate_package(first)

    with pytest.raises(IntegrityError), transaction.atomic():
        _write_installed_package_lifecycle(second.pk, state=ACTIVE)

    assert InstalledPackage.objects.filter(
        package_type=first.package_type, source_public_id=first.source_public_id, state=ACTIVE
    ).count() == 1


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_archived_package_cannot_be_directly_reactivated(quiz_world, keypair):
    """Task 12 corrective §33: ARCHIVED -> ACTIVE is only ever reached
    through rollback_package(), never through activate_package() again --
    this closes the previous_package cycle footgun a direct
    archived->active transition would otherwise allow."""
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    activate_package(first)

    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )
    activate_package(second)

    reloaded_first = InstalledPackage.objects.get(pk=first.pk)
    assert reloaded_first.state == ARCHIVED

    with pytest.raises(PackageActivationError):
        activate_package(reloaded_first)

    assert InstalledPackage.objects.get(pk=first.pk).state == ARCHIVED
    assert InstalledPackage.objects.get(pk=second.pk).state == ACTIVE


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_incomplete_fabricated_package_fails_completeness_check_on_activation():
    """Defense in depth (Task 12 corrective §23-25): a row introduced
    through the low-level trusted-insertion seam but missing its
    publication artifact (something ordinary ORM fabrication can no
    longer produce at all, now that public creation is blocked outright)
    must still fail closed on activation."""
    import uuid
    from datetime import UTC, datetime

    from linuxlab.sync.services import _insert_trusted

    package = _insert_trusted(
        InstalledPackage,
        package_type="learning_path",
        source_public_id=uuid.uuid4(),
        package_version=1,
        payload_sha256="0" * 64,
        schema_version=1,
        minimum_linuxlab_version="0.1.0",
        signature_algorithm="ed25519",
        manifest_created_at=datetime.now(UTC),
        state=STAGED,
    )

    with pytest.raises(PackageActivationError, match="publication artifact"):
        activate_package(package)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activation_failure_leaves_no_partial_transition(quiz_world, keypair):
    """Task 12 corrective §61: force a failure after the old active
    package would already have been archived but before the candidate's
    own activation commits. The whole transaction must roll back --
    neither package may end up in a partially-transitioned state."""
    from linuxlab.sync import services as sync_services

    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    activate_package(first)

    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )

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
        activate_package(second)

    assert InstalledPackage.objects.get(pk=first.pk).state == ACTIVE
    assert InstalledPackage.objects.get(pk=second.pk).state == STAGED


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_active_package_cannot_be_deleted(quiz_world, keypair):
    private_key, public_key = keypair
    package = _stage(quiz_world, private_key, public_key)
    activate_package(package)

    with pytest.raises(ValueError, match="cannot be deleted"):
        package.delete()
    with pytest.raises(ValueError, match="cannot be deleted"):
        InstalledPackage.objects.filter(pk=package.pk).delete()

    reloaded = InstalledPackage.objects.get(pk=package.pk)
    assert reloaded.state == ACTIVE
    assert reloaded.activities.count() == 1
    assert reloaded.questions.count() == 1
    assert reloaded.publication is not None


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activation_never_re_verifies_the_package(quiz_world, keypair):
    """Activation is a local lifecycle transition on already-trusted staged
    rows -- it must not require the trusted public key or re-run Task 11
    verification every time a user activates/rolls back."""
    private_key, public_key = keypair
    package = _stage(quiz_world, private_key, public_key)

    with patch(
        "linuxlab.sync.services.verify_package",
        side_effect=AssertionError("activate_package must never call verify_package"),
    ):
        activate_package(package)

    assert InstalledPackage.objects.get(pk=package.pk).state == ACTIVE
