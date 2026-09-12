"""Task 12 import_package(): verify-before-write, idempotency, identity
conflict, transactional all-or-nothing staging, node-capable-profile
gating, and the verification-bypass adversarial case (a manually
constructed VerifiedPackage must never reach the database through the
public API)."""

import json
import struct
import zipfile
from hashlib import sha256
from io import BytesIO
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import override_settings

from linuxlab.publication.package import (
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    SIGNATURE_MEMBER,
    build_package,
)
from linuxlab.publication.signing import sign_bytes
from linuxlab.publication.snapshots import canonical_json_bytes
from linuxlab.sync.models import (
    STAGED,
    InstalledActivityVersion,
    InstalledPackage,
    InstalledPublication,
    InstalledQuestionGrading,
    InstalledQuestionVersion,
)
from linuxlab.sync.services import (
    PackageIdentityConflict,
    PackageImportError,
    import_package,
)

from .conftest import build_quiz_world

pytestmark = pytest.mark.django_db(transaction=True)


def _package_bytes(world, private_key):
    return build_package(world["snapshot"], world["activity_registry"], private_key)


def _forge_identity(package_bytes, *, package_uuid, package_version, private_key):
    """Rebuild+re-sign a package around a forged identity (manifest
    package_uuid/package_version AND the matching payload.publication
    fields, since verify_package cross-checks the two) using the SAME
    trusted private key -- a real attacker without that key would already
    be blocked by the signature alone; this isolates identity-collision
    rejection as its own, independently real failure mode."""
    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        manifest = json.loads(archive.read(MANIFEST_MEMBER))
        payload = json.loads(archive.read(PAYLOAD_MEMBER))

    manifest["package_uuid"] = str(package_uuid)
    manifest["package_version"] = package_version
    payload["publication"]["source_public_id"] = str(package_uuid)
    payload["publication"]["version"] = package_version

    payload_bytes = canonical_json_bytes(payload)
    manifest["payload_sha256"] = sha256(payload_bytes).hexdigest()
    manifest_bytes = canonical_json_bytes(manifest)
    signature = sign_bytes(manifest_bytes + payload_bytes, private_key)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, manifest_bytes)
        archive.writestr(PAYLOAD_MEMBER, payload_bytes)
        archive.writestr(SIGNATURE_MEMBER, signature)
    return buffer.getvalue()


def _assert_no_installed_rows():
    assert InstalledPackage.objects.count() == 0
    assert InstalledPublication.objects.count() == 0
    assert InstalledActivityVersion.objects.count() == 0
    assert InstalledQuestionVersion.objects.count() == 0
    assert InstalledQuestionGrading.objects.count() == 0


# --- node-capable profile gate ---------------------------------------------


def test_import_refused_when_profile_cannot_import_packages(quiz_world, keypair):
    private_key, public_key = keypair
    package_bytes = _package_bytes(quiz_world, private_key)

    # Test settings default LINUXLAB_CAN_IMPORT_PACKAGES to False.
    with pytest.raises(PackageImportError, match="profile"):
        import_package(package_bytes, quiz_world["activity_registry"], public_key)

    _assert_no_installed_rows()


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_import_succeeds_on_a_node_capable_profile(quiz_world, keypair):
    private_key, public_key = keypair
    package_bytes = _package_bytes(quiz_world, private_key)

    package = import_package(package_bytes, quiz_world["activity_registry"], public_key)

    assert package.state == STAGED


# --- verify before database writes -----------------------------------------


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_invalid_signature_creates_no_rows(quiz_world, keypair):
    _private_key, public_key = keypair
    wrong_private_key = Ed25519PrivateKey.generate()
    tampered_bytes = _package_bytes(quiz_world, wrong_private_key)

    with pytest.raises(PackageImportError):
        import_package(tampered_bytes, quiz_world["activity_registry"], public_key)

    _assert_no_installed_rows()


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_malformed_bytes_create_no_rows(quiz_world, keypair):
    _private_key, public_key = keypair

    with pytest.raises(PackageImportError):
        import_package(b"not a package at all", quiz_world["activity_registry"], public_key)

    _assert_no_installed_rows()


def _corrupt_deflate_member(package_bytes, member_name):
    """Same technique as linuxlab/publication/tests/test_package.py::
    _corrupt_deflate_member (independent review, Task 4 fix-round-1):
    overwrite a member's ON-DISK DEFLATE-compressed bytes in place, so the
    ZIP container stays structurally valid but decompressing this member
    raises a real zlib.error, not a zipfile.BadZipFile."""
    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        info = archive.getinfo(member_name)
    assert info.compress_type == zipfile.ZIP_DEFLATED

    header_offset = info.header_offset
    filename_len, extra_len = struct.unpack_from("<HH", package_bytes, header_offset + 26)
    data_start = header_offset + 30 + filename_len + extra_len
    compressed_size = info.compress_size

    corrupted = bytearray(package_bytes)
    corrupted[data_start : data_start + compressed_size] = b"\xff" * compressed_size
    return bytes(corrupted)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_malformed_deflate_member_creates_no_rows(quiz_world, keypair):
    """Verifier normalization (Task 4 fix-round-1, Important I1): a
    well-formed ZIP container holding an invalid DEFLATE byte stream for
    one member must surface through the exact same PackageImportError
    boundary as any other malformed package, not a raw zlib.error."""
    private_key, public_key = keypair
    package_bytes = _package_bytes(quiz_world, private_key)
    corrupted = _corrupt_deflate_member(package_bytes, PAYLOAD_MEMBER)

    with pytest.raises(PackageImportError) as excinfo:
        import_package(corrupted, quiz_world["activity_registry"], public_key)

    assert "zlib" not in str(excinfo.value).lower()
    _assert_no_installed_rows()


# --- staging is all-or-nothing ----------------------------------------------


def _failing_insert_for(target_model_cls):
    """A side_effect for linuxlab.sync.services._insert_trusted that
    delegates to the REAL implementation for every model except
    `target_model_cls`, which raises instead -- forcing a failure at a
    specific point deep inside _stage_verified_package()'s insert
    sequence while still exercising the real trusted insertion path for
    everything before it. `original` is captured here, before patch()
    replaces the module attribute, so it is genuinely the unpatched
    implementation."""
    from linuxlab.sync import services as sync_services

    original = sync_services._insert_trusted

    def _side_effect(model_cls, **fields):
        if model_cls is target_model_cls:
            raise RuntimeError("simulated failure")
        return original(model_cls, **fields)

    return _side_effect


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_partial_stage_failure_leaves_no_rows_and_active_package_untouched(quiz_world, keypair):
    private_key, public_key = keypair
    package_bytes = _package_bytes(quiz_world, private_key)

    with (
        patch(
            "linuxlab.sync.services._insert_trusted",
            side_effect=_failing_insert_for(InstalledQuestionGrading),
        ),
        pytest.raises(RuntimeError),
    ):
        import_package(package_bytes, quiz_world["activity_registry"], public_key)

    _assert_no_installed_rows()


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_partial_stage_failure_does_not_disturb_an_existing_active_package(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first_bytes = _package_bytes(quiz_world, private_key)
    first_package = import_package(first_bytes, registry, public_key)

    from .conftest import republish

    second_snapshot = republish(quiz_world)
    second_bytes = build_package(second_snapshot, registry, private_key)

    with (
        patch(
            "linuxlab.sync.services._insert_trusted",
            side_effect=_failing_insert_for(InstalledQuestionGrading),
        ),
        pytest.raises(RuntimeError),
    ):
        import_package(second_bytes, registry, public_key)

    assert InstalledPackage.objects.count() == 1
    reloaded_first = InstalledPackage.objects.get(pk=first_package.pk)
    assert reloaded_first.state == STAGED
    assert reloaded_first.package_version == first_package.package_version


# --- idempotent import -------------------------------------------------------


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_importing_the_same_package_twice_is_idempotent(quiz_world, keypair):
    private_key, public_key = keypair
    package_bytes = _package_bytes(quiz_world, private_key)
    registry = quiz_world["activity_registry"]

    first = import_package(package_bytes, registry, public_key)
    second = import_package(package_bytes, registry, public_key)

    assert first.pk == second.pk
    assert InstalledPackage.objects.count() == 1
    assert InstalledPublication.objects.count() == 1
    assert InstalledActivityVersion.objects.count() == 1
    assert InstalledQuestionVersion.objects.count() == 1
    assert InstalledQuestionGrading.objects.count() == 1


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_restaging_an_active_package_does_not_change_its_state(quiz_world, keypair):
    from linuxlab.sync.services import activate_package

    private_key, public_key = keypair
    package_bytes = _package_bytes(quiz_world, private_key)
    registry = quiz_world["activity_registry"]

    package = import_package(package_bytes, registry, public_key)
    activated = activate_package(package)
    assert activated.state == "active"

    restaged = import_package(package_bytes, registry, public_key)

    assert restaged.pk == activated.pk
    assert InstalledPackage.objects.get(pk=activated.pk).state == "active"


# --- identity/hash conflict --------------------------------------------------


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_same_identity_different_payload_hash_is_rejected():
    """Two independently built, individually well-formed packages
    (world_a, world_b) forged to share the SAME
    (package_type, source_public_id, package_version) identity but carry
    different real content/payload_sha256 -- properly re-signed with the
    SAME trusted key so this exercises identity-collision rejection, not
    signature rejection (Task 12 §7-8, §39)."""
    world_a = build_quiz_world()
    world_b = build_quiz_world()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    package_a_bytes = _package_bytes(world_a, private_key)
    package_a = import_package(package_a_bytes, world_a["activity_registry"], public_key)

    package_b_bytes = _package_bytes(world_b, private_key)
    forged_bytes = _forge_identity(
        package_b_bytes,
        package_uuid=package_a.source_public_id,
        package_version=package_a.package_version,
        private_key=private_key,
    )

    with pytest.raises(PackageIdentityConflict):
        import_package(forged_bytes, world_b["activity_registry"], public_key)

    assert InstalledPackage.objects.count() == 1
    reloaded = InstalledPackage.objects.get(pk=package_a.pk)
    assert reloaded.payload_sha256 == package_a.payload_sha256


# --- staging never activates -------------------------------------------------


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_staging_a_new_version_does_not_touch_an_existing_active_package(quiz_world, keypair):
    from linuxlab.sync.services import activate_package

    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first_bytes = _package_bytes(quiz_world, private_key)
    first_package = import_package(first_bytes, registry, public_key)
    activate_package(first_package)

    from .conftest import republish

    second_snapshot = republish(quiz_world)
    second_bytes = build_package(second_snapshot, registry, private_key)
    second_package = import_package(second_bytes, registry, public_key)

    reloaded_first = InstalledPackage.objects.get(pk=first_package.pk)
    assert reloaded_first.state == "active"
    assert second_package.state == STAGED


# --- counterfeit VerifiedPackage cannot bypass verification -----------------


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_import_package_signature_only_accepts_raw_bytes_and_trusted_inputs():
    """import_package()'s signature has no parameter for a pre-built
    VerifiedPackage -- there is no supported way to pass a caller-
    constructed one into the public entry point at all."""
    import inspect

    from linuxlab.sync import services

    signature = inspect.signature(services.import_package)
    assert list(signature.parameters) == [
        "package_bytes",
        "activity_registry",
        "trusted_public_key",
    ]


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_manually_constructed_verified_package_cannot_reach_the_database_via_import_package(
    quiz_world, keypair
):
    """A manually constructed VerifiedPackage (Python objects can always
    be built by hand -- Task 12 §5/§42) is not cryptographic proof of
    anything. Handing one to the public import_package() where raw bytes
    are expected does not stage it: Task 11's own bytes handling rejects
    it outright, and no InstalledPackage/artifact rows are ever created."""
    from linuxlab.publication.package import VerifiedPackage, verify_package

    private_key, public_key = keypair
    package_bytes = _package_bytes(quiz_world, private_key)
    genuine = verify_package(package_bytes, quiz_world["activity_registry"], public_key)

    counterfeit = VerifiedPackage(
        manifest=genuine.manifest,
        package_bytes=b"forged bytes, never actually verified",
        payload_bytes=genuine.payload_bytes,
    )

    with pytest.raises((PackageImportError, TypeError)):
        import_package(counterfeit, quiz_world["activity_registry"], public_key)

    _assert_no_installed_rows()


# --- archived-package restage (Task 12 corrective §37) ----------------------


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_restaging_an_archived_package_does_not_change_its_state(quiz_world, keypair):
    from linuxlab.sync.models import ARCHIVED
    from linuxlab.sync.services import activate_package

    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    first_bytes = _package_bytes(quiz_world, private_key)
    first = import_package(first_bytes, registry, public_key)
    activate_package(first)

    from .conftest import republish

    second_snapshot = republish(quiz_world)
    second = import_package(
        build_package(second_snapshot, registry, private_key), registry, public_key
    )
    activate_package(second)

    first_reloaded = InstalledPackage.objects.get(pk=first.pk)
    assert first_reloaded.state == ARCHIVED

    restaged = import_package(first_bytes, registry, public_key)

    assert restaged.pk == first.pk
    assert InstalledPackage.objects.get(pk=first.pk).state == ARCHIVED
    assert InstalledPublication.objects.filter(package=first).count() == 1
    assert InstalledActivityVersion.objects.filter(package=first).count() == 1


# --- concurrent import races (Task 12 corrective §38-40) --------------------


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_concurrent_identical_first_import_resolves_to_one_package(quiz_world, keypair):
    """Simulate a concurrent transaction committing the exact same new
    package identity between our own existence check and our own insert
    attempt: patch _find_installed_package to miss on its first call only
    (as if our SELECT ran before the other transaction's INSERT
    committed), so our own insert hits the real unique constraint and the
    recovery path in _stage_verified_package() must resolve to the
    already-committed row rather than raising a raw IntegrityError."""
    private_key, public_key = keypair
    package_bytes = _package_bytes(quiz_world, private_key)
    registry = quiz_world["activity_registry"]

    winner = import_package(package_bytes, registry, public_key)

    from linuxlab.sync import services as sync_services

    real_find = sync_services._find_installed_package
    call_count = {"n": 0}

    def missing_once(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_find(*args, **kwargs)

    with patch("linuxlab.sync.services._find_installed_package", side_effect=missing_once):
        loser_result = import_package(package_bytes, registry, public_key)

    assert loser_result.pk == winner.pk
    assert InstalledPackage.objects.count() == 1
    assert InstalledPublication.objects.count() == 1


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_concurrent_different_hash_import_resolves_to_conflict_not_integrity_error():
    """Same race as above, but the racing package carries a different
    payload hash for the same identity: the loser must still resolve to a
    clean PackageIdentityConflict, never a raw IntegrityError, and must
    never overwrite the winner."""
    world_a = build_quiz_world()
    world_b = build_quiz_world()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    winner_bytes = _package_bytes(world_a, private_key)
    winner = import_package(winner_bytes, world_a["activity_registry"], public_key)

    loser_bytes = _package_bytes(world_b, private_key)
    forged_bytes = _forge_identity(
        loser_bytes,
        package_uuid=winner.source_public_id,
        package_version=winner.package_version,
        private_key=private_key,
    )

    from linuxlab.sync import services as sync_services

    real_find = sync_services._find_installed_package
    call_count = {"n": 0}

    def missing_once(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_find(*args, **kwargs)

    with (
        patch("linuxlab.sync.services._find_installed_package", side_effect=missing_once),
        pytest.raises(PackageIdentityConflict),
    ):
        import_package(forged_bytes, world_b["activity_registry"], public_key)

    assert InstalledPackage.objects.count() == 1
    assert InstalledPackage.objects.get(pk=winner.pk).payload_sha256 == winner.payload_sha256
