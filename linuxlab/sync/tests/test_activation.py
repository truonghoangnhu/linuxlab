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
    """DB-level invariant test: even bypassing the service's own lock by
    racing two raw activations against the same scope, the partial unique
    index is the final backstop -- at most one row can ever be committed
    ACTIVE for one source."""
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first = _stage(quiz_world, private_key, public_key)
    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )

    activate_package(first)

    with pytest.raises(IntegrityError), transaction.atomic():
        InstalledPackage.objects.filter(pk=second.pk).update(state=ACTIVE)

    assert InstalledPackage.objects.filter(
        package_type=first.package_type, source_public_id=first.source_public_id, state=ACTIVE
    ).count() == 1


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
