"""Task 13 corrective: seal RuntimePackageContext opening provenance.

An independent review found that _rebind_runtime_context() only compared
persisted-identity fields, never proving a context was actually minted by
open_runtime_context() while its package was ACTIVE. A manually
constructed RuntimePackageContext carrying an ARCHIVED or STAGED
package's exact persisted identity therefore passed rebind undetected.

This completes T13-D10's existing trust semantics (a context is "a
locator + identity claim, not a trust token") with same-process HMAC
provenance -- it does not re-check ACTIVE (T13-D11 is unaffected).
"""

import uuid
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import override_settings

from linuxlab.publication.package import build_package
from linuxlab.runtime.content import get_runtime_publication
from linuxlab.runtime.context import open_runtime_context
from linuxlab.runtime.dto import RuntimePackageContext
from linuxlab.runtime.exceptions import RuntimeContextInvalid
from linuxlab.sync.models import ARCHIVED, InstalledPackage
from linuxlab.sync.services import activate_package, import_package

from .conftest import build_quiz_world, republish, stage

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_forged_exact_match_archived_context_is_rejected(installed_active_world):
    """Reported vulnerability, reproduced: an exact-match context for a
    package that is now ARCHIVED, but was never minted by
    open_runtime_context(), must be rejected."""
    world = installed_active_world
    registry = world["activity_registry"]
    private_key, public_key = world["private_key"], world["public_key"]
    v1 = world["package"]

    v2_snapshot = republish(world)
    v2 = import_package(build_package(v2_snapshot, registry, private_key), registry, public_key)
    activate_package(v2)

    v1_reloaded = InstalledPackage.objects.get(pk=v1.pk)
    assert v1_reloaded.state == ARCHIVED

    forged = RuntimePackageContext(
        package_pk=v1_reloaded.pk,
        package_type=v1_reloaded.package_type,
        source_public_id=v1_reloaded.source_public_id,
        package_version=v1_reloaded.package_version,
        payload_sha256=v1_reloaded.payload_sha256,
        _provenance_tag=b"forged, never minted by open_runtime_context()",
    )

    with pytest.raises(RuntimeContextInvalid):
        get_runtime_publication(forged)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_forged_exact_match_staged_context_is_rejected():
    """Same vulnerability, STAGED variant: a package that was staged but
    never activated at all, so open_runtime_context() could never
    legitimately have minted a context for it."""
    world = build_quiz_world()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    staged = stage(world, private_key, public_key)

    forged = RuntimePackageContext(
        package_pk=staged.pk,
        package_type=staged.package_type,
        source_public_id=staged.source_public_id,
        package_version=staged.package_version,
        payload_sha256=staged.payload_sha256,
        _provenance_tag=b"forged, never minted by open_runtime_context()",
    )

    with pytest.raises(RuntimeContextInvalid):
        get_runtime_publication(forged)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_legitimate_context_still_resolves_after_active_becomes_archived(installed_active_world):
    """Regression guard for T13-D11/T13-D14: the corrective must not
    reintroduce an ACTIVE recheck. A context legitimately minted while
    v1 was ACTIVE must keep working after v2 is activated and v1
    becomes ARCHIVED."""
    world = installed_active_world
    registry = world["activity_registry"]
    private_key, public_key = world["private_key"], world["public_key"]
    v1 = world["package"]

    ctx = open_runtime_context(v1.package_type, v1.source_public_id)

    v2_snapshot = republish(world)
    v2 = import_package(build_package(v2_snapshot, registry, private_key), registry, public_key)
    activate_package(v2)
    assert InstalledPackage.objects.get(pk=v1.pk).state == ARCHIVED

    publication = get_runtime_publication(ctx)
    assert publication.version == world["snapshot"].version


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
@pytest.mark.parametrize(
    "field_name,tampered_value",
    [
        ("package_pk", 999999),
        ("package_type", "not_learning_path"),
        ("source_public_id", uuid.uuid4()),
        ("package_version", 999),
        ("payload_sha256", "f" * 64),
    ],
)
def test_tampering_any_identity_field_without_reminting_is_rejected(
    installed_active_world, field_name, tampered_value
):
    """The HMAC binds every identity field: altering any single one
    without re-minting the tag must invalidate the whole context."""
    world = installed_active_world
    package = world["package"]
    ctx = open_runtime_context(package.package_type, package.source_public_id)

    tampered = replace(ctx, **{field_name: tampered_value})

    with pytest.raises(RuntimeContextInvalid):
        get_runtime_publication(tampered)
