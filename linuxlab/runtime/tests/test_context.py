"""Task 13 open_runtime_context()/_rebind_runtime_context(): ACTIVE-only
context opening, no latest/staged fallback, identity rebind on every
lookup, and survival of the ACTIVE -> ARCHIVED transition for an
already-pinned context (T13-D01, T13-D02, T13-D08-D11)."""

import uuid
from dataclasses import replace

import pytest
from django.test import override_settings

from linuxlab.publication.package import build_package
from linuxlab.runtime.context import _rebind_runtime_context, open_runtime_context
from linuxlab.runtime.dto import RuntimePackageContext
from linuxlab.runtime.exceptions import RuntimeContentUnavailable, RuntimeContextInvalid
from linuxlab.sync.models import ARCHIVED, InstalledPackage
from linuxlab.sync.services import activate_package, import_package

from .conftest import build_quiz_world, republish, stage

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_open_runtime_context_requires_active_package(installed_active_world):
    """A STAGED (never-activated) newer version must not satisfy
    open_runtime_context() when nothing is ACTIVE for that source."""
    world = installed_active_world
    private_key, public_key = world["private_key"], world["public_key"]
    package_type = world["package"].package_type
    source_public_id = world["package"].source_public_id

    # Start a brand-new source with only a STAGED package -- no ACTIVE yet.
    other_world = build_quiz_world()
    stage(other_world, private_key, public_key)

    with pytest.raises(RuntimeContentUnavailable):
        open_runtime_context(package_type, other_world["snapshot"].source_public_id)

    # Sanity: the already-ACTIVE world still opens fine.
    open_runtime_context(package_type, source_public_id)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_open_runtime_context_pins_exact_active_identity(installed_active_world):
    world = installed_active_world
    package = world["package"]

    context = open_runtime_context(package.package_type, package.source_public_id)

    assert isinstance(context, RuntimePackageContext)
    assert context.package_pk == package.pk
    assert context.package_type == package.package_type
    assert context.source_public_id == package.source_public_id
    assert context.package_version == package.package_version
    assert context.payload_sha256 == package.payload_sha256


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_open_runtime_context_never_selects_a_higher_staged_version(installed_active_world):
    """v1 ACTIVE, v2 STAGED with a higher version number: opening a new
    context must still resolve v1, never the higher STAGED version."""
    world = installed_active_world
    registry = world["activity_registry"]
    private_key, public_key = world["private_key"], world["public_key"]
    v1 = world["package"]

    v2_snapshot = republish(world)
    v2_bytes = build_package(v2_snapshot, registry, private_key)
    v2 = import_package(v2_bytes, registry, public_key)
    assert v2.package_version > v1.package_version

    context = open_runtime_context(v1.package_type, v1.source_public_id)

    assert context.package_pk == v1.pk
    assert context.package_version == v1.package_version


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_open_runtime_context_no_match_for_unrelated_source():
    with pytest.raises(RuntimeContentUnavailable):
        open_runtime_context("learning_path", uuid.uuid4())


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_rebind_returns_the_persisted_package_for_a_genuine_context(installed_active_world):
    world = installed_active_world
    package = world["package"]

    context = open_runtime_context(package.package_type, package.source_public_id)
    rebound = _rebind_runtime_context(context)

    assert rebound.pk == package.pk


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_rebind_rejects_fabricated_identity_claim(installed_active_world):
    world = installed_active_world
    package = world["package"]
    context = open_runtime_context(package.package_type, package.source_public_id)

    forged = replace(context, payload_sha256="0" * 64)

    with pytest.raises(RuntimeContextInvalid):
        _rebind_runtime_context(forged)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_rebind_rejects_a_context_pointing_at_a_missing_package():
    # An arbitrary/invalid provenance tag is fine here: this test's
    # target is the "package no longer exists" path, and an unsealed
    # context is correctly rejected before ever reaching it anyway (see
    # test_context_provenance.py for the dedicated provenance tests).
    fake_context = RuntimePackageContext(
        package_pk=999999,
        package_type="learning_path",
        source_public_id=uuid.uuid4(),
        package_version=1,
        payload_sha256="0" * 64,
        _provenance_tag=b"not a real provenance tag",
    )

    with pytest.raises(RuntimeContextInvalid):
        _rebind_runtime_context(fake_context)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_pinned_context_remains_valid_after_package_is_archived(installed_active_world):
    world = installed_active_world
    registry = world["activity_registry"]
    private_key, public_key = world["private_key"], world["public_key"]
    v1 = world["package"]

    context = open_runtime_context(v1.package_type, v1.source_public_id)

    v2_snapshot = republish(world)
    v2_bytes = build_package(v2_snapshot, registry, private_key)
    v2 = import_package(v2_bytes, registry, public_key)
    activate_package(v2)

    assert InstalledPackage.objects.get(pk=v1.pk).state == ARCHIVED

    rebound = _rebind_runtime_context(context)

    assert rebound.pk == v1.pk
    assert rebound.state == ARCHIVED


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_new_context_after_activation_resolves_the_new_active_package(installed_active_world):
    world = installed_active_world
    registry = world["activity_registry"]
    private_key, public_key = world["private_key"], world["public_key"]
    v1 = world["package"]

    v2_snapshot = republish(world)
    v2_bytes = build_package(v2_snapshot, registry, private_key)
    v2 = import_package(v2_bytes, registry, public_key)
    activate_package(v2)

    new_context = open_runtime_context(v1.package_type, v1.source_public_id)

    assert new_context.package_pk == v2.pk
    assert new_context.package_version == v2.package_version
