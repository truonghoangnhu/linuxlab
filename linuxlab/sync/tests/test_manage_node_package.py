import struct
import uuid
import zipfile
from io import BytesIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from linuxlab.publication.package import PAYLOAD_MEMBER
from linuxlab.sync.models import InstalledPackage

pytestmark = pytest.mark.django_db(transaction=True)


def _tamper_package_payload(package_bytes):
    """Same established technique as linuxlab/publication/tests/
    test_package.py (_rewrite_member + a one-byte payload flip): rebuild
    the archive with the PAYLOAD member's bytes altered, leaving the
    original signature over the UNTAMPERED payload in place, so
    verification is exercised against a genuine hash/signature mismatch
    rather than a raw archive-corruption failure."""
    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}

    payload = bytearray(members[PAYLOAD_MEMBER])
    payload[0] ^= 0xFF
    members[PAYLOAD_MEMBER] = bytes(payload)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _corrupt_deflate_member(package_bytes, member_name):
    """Same technique as linuxlab/publication/tests/test_package.py::
    _corrupt_deflate_member and linuxlab/sync/tests/test_import_package.py
    ::_corrupt_deflate_member (independent review, Task 4 fix-round-1,
    Important I1): overwrite a member's ON-DISK DEFLATE-compressed bytes
    in place, so the ZIP container stays structurally valid but
    decompressing this member raises a real zlib.error, not a
    zipfile.BadZipFile -- exercising the exact verifier-boundary
    normalization this fix-round adds, through the real command."""
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


@pytest.fixture
def signed_package(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from linuxlab.activities.default_registry import build_default_activity_registry
    from linuxlab.content.models import LearningPathPage
    from linuxlab.publication.package import build_package
    from linuxlab.publication.snapshots import publish_learning_path

    from ._wagtail_bootstrap import get_or_create_root_page

    root = get_or_create_root_page()
    path = LearningPathPage(title="Intro", slug=f"intro-{uuid.uuid4().hex[:8]}")
    root.add_child(instance=path)

    registry = build_default_activity_registry()
    snapshot = publish_learning_path(path, registry)

    private_key = Ed25519PrivateKey.generate()
    package_bytes = build_package(snapshot, registry, private_key)

    trust_path = tmp_path / "package-signing-public.key"
    trust_path.write_bytes(
        private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", trust_path)

    package_path = tmp_path / "package.llpkg"
    package_path.write_bytes(package_bytes)

    return {
        "path": package_path,
        "bytes": package_bytes,
        "source_public_id": snapshot.source_public_id,
        "version": snapshot.version,
        "private_key": private_key,
        "trust_path": trust_path,
    }


@pytest.fixture
def rollback_world(signed_package, tmp_path):
    from linuxlab.activities.default_registry import build_default_activity_registry
    from linuxlab.content.models import LearningPathPage
    from linuxlab.publication.package import build_package
    from linuxlab.publication.snapshots import publish_learning_path

    page = LearningPathPage.objects.get(public_id=signed_package["source_public_id"])
    registry = build_default_activity_registry()
    snapshot_b = publish_learning_path(page, registry)
    package_bytes_b = build_package(snapshot_b, registry, signed_package["private_key"])

    package_path_b = tmp_path / "package-b.llpkg"
    package_path_b.write_bytes(package_bytes_b)

    return {**signed_package, "path_b": package_path_b, "version_b": snapshot_b.version}


# --- verify -------------------------------------------------------------


def test_verify_succeeds_on_valid_package(settings, signed_package, capsys):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

    out = capsys.readouterr().out
    assert out == (
        "Operation: verify\n"
        "PackageType: learning_path\n"
        f"Source: {signed_package['source_public_id']}\n"
        f"Version: {signed_package['version']}\n"
    )


def test_verify_causes_zero_installed_package_mutation(settings, signed_package):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))
    assert InstalledPackage.objects.count() == 0


def test_verify_rejects_wrong_key(settings, signed_package, tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    wrong_public = Ed25519PrivateKey.generate().public_key()
    wrong_trust_path = tmp_path / "wrong-public.key"
    wrong_trust_path.write_bytes(
        wrong_public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", wrong_trust_path)

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

    # A syntactically valid but non-matching trust key is a signature
    # verification failure, NOT a trust-configuration error (Amendment C
    # acceptance / spec Section 5a) -- pin the stable command-level
    # category, not the underlying crypto library's exact wording.
    message = str(excinfo.value)
    assert "package verification failed" in message
    assert "trust anchor" not in message
    assert InstalledPackage.objects.count() == 0


def test_verify_rejects_tampered_package(settings, signed_package):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    tampered_path = signed_package["path"].with_name("tampered.llpkg")
    tampered_path.write_bytes(_tamper_package_payload(signed_package["bytes"]))

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_node_package", "verify", "--file", str(tampered_path))

    message = str(excinfo.value)
    assert "package verification failed" in message
    assert "trust anchor" not in message
    assert InstalledPackage.objects.count() == 0


def test_verify_rejects_malformed_deflate_member(settings, signed_package):
    """Independent review Important I1: a malformed compressed package
    member must surface as a normal package-verification CommandError,
    never a raw zlib.error, through the real (unmocked) verify_package()."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    corrupted_path = signed_package["path"].with_name("corrupted.llpkg")
    corrupted_path.write_bytes(_corrupt_deflate_member(signed_package["bytes"], PAYLOAD_MEMBER))

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_node_package", "verify", "--file", str(corrupted_path))

    message = str(excinfo.value)
    assert "zlib" not in message.lower()
    assert "package verification failed" in message
    assert InstalledPackage.objects.count() == 0


def test_verify_rejects_missing_package_file(settings, signed_package, tmp_path):
    """M1: file-read boundary after a successful trust load."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    with pytest.raises(CommandError):
        call_command(
            "manage_node_package", "verify", "--file", str(tmp_path / "does-not-exist.llpkg")
        )

    assert InstalledPackage.objects.count() == 0


def test_verify_rejects_directory_as_package_file(settings, signed_package, tmp_path):
    """M1: file-read boundary after a successful trust load, for a path
    that exists but is not a regular file."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    a_directory = tmp_path / "a-directory"
    a_directory.mkdir()

    with pytest.raises(CommandError):
        call_command("manage_node_package", "verify", "--file", str(a_directory))

    assert InstalledPackage.objects.count() == 0


def test_verify_rejects_malformed_archive(settings, tmp_path, signed_package):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    not_a_package = tmp_path / "not-a-package.llpkg"
    not_a_package.write_bytes(b"not a zip archive")

    with pytest.raises(CommandError):
        call_command("manage_node_package", "verify", "--file", str(not_a_package))


def test_verify_rejects_missing_trust_anchor(settings, signed_package, tmp_path, monkeypatch):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    monkeypatch.setattr(
        "linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "does-not-exist.key"
    )
    with pytest.raises(CommandError):
        call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

    assert InstalledPackage.objects.count() == 0


def test_verify_rejects_wrong_profile_before_trust_loading(
    settings, signed_package, monkeypatch
):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
    called = []
    monkeypatch.setattr(
        "linuxlab.sync.management.commands.manage_node_package.load_package_trusted_public_key",
        lambda: called.append(True),
    )

    with pytest.raises(CommandError):
        call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

    assert called == []


def test_verify_rejects_wrong_profile_with_nonexistent_file_and_gives_capability_error(
    settings, monkeypatch
):
    """Adversarial (Task 4 corrective Section 5): the capability guard must
    fail closed before trust loading OR package-file reading even run, so
    the reported failure is the capability rejection -- never a file-not-
    found or trust error -- regardless of how bogus the rest of the
    invocation is."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
    trust_called = []
    monkeypatch.setattr(
        "linuxlab.sync.management.commands.manage_node_package.load_package_trusted_public_key",
        lambda: trust_called.append(True),
    )

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_node_package", "verify", "--file", "/definitely/does/not/exist.llpkg"
        )

    assert "not permitted on this deployment profile" in str(excinfo.value)
    assert trust_called == []


def test_verify_leaves_preexisting_installed_package_inventory_unchanged(
    settings, signed_package
):
    """M1: verify's read-only guarantee proven against a NON-empty
    inventory, not merely an empty database. A package is staged first
    (a real InstalledPackage row now exists); verify is then run again on
    the same valid package, and the existing row's identity/state/pk must
    be completely unaffected -- verify must never create, restage, or
    otherwise touch it."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    installed_before = InstalledPackage.objects.get()
    identity_before = (
        installed_before.pk,
        installed_before.package_type,
        installed_before.source_public_id,
        installed_before.package_version,
        installed_before.state,
    )

    call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

    assert InstalledPackage.objects.count() == 1
    installed_after = InstalledPackage.objects.get()
    identity_after = (
        installed_after.pk,
        installed_after.package_type,
        installed_after.source_public_id,
        installed_after.package_version,
        installed_after.state,
    )
    assert identity_after == identity_before


def test_verify_trust_failure_precedes_missing_file_error(settings, tmp_path, monkeypatch):
    """Ordering (spec Section 5a / Amendment C, plan Task 4): trust-anchor
    loading happens strictly before the package file is read, so an
    invalid trust configuration is reported even when the --file path
    also does not exist."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    monkeypatch.setattr(
        "linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "does-not-exist.key"
    )

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_node_package",
            "verify",
            "--file",
            str(tmp_path / "also-does-not-exist.llpkg"),
        )

    assert "trust anchor" in str(excinfo.value)


# --- import ---------------------------------------------------------------


def test_import_stages_package_and_reports_canonical_identity(settings, signed_package, capsys):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

    out = capsys.readouterr().out
    assert out == (
        "Operation: import\n"
        "PackageType: learning_path\n"
        f"Source: {signed_package['source_public_id']}\n"
        f"Version: {signed_package['version']}\n"
        "State: staged\n"
    )


def test_import_rejects_wrong_key_with_zero_mutation(
    settings, signed_package, tmp_path, monkeypatch
):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    wrong_public = Ed25519PrivateKey.generate().public_key()
    wrong_trust_path = tmp_path / "wrong-public.key"
    wrong_trust_path.write_bytes(
        wrong_public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", wrong_trust_path)

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

    # Same distinction as verify: a syntactically valid but non-matching
    # trust key fails as an import/verification rejection, not a
    # trust-configuration error.
    message = str(excinfo.value)
    assert "package import failed" in message
    assert "trust anchor" not in message
    assert InstalledPackage.objects.count() == 0


def test_import_rejects_missing_trust_configuration_with_zero_mutation(
    settings, signed_package, tmp_path, monkeypatch
):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    monkeypatch.setattr(
        "linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "does-not-exist.key"
    )

    with pytest.raises(CommandError):
        call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

    assert InstalledPackage.objects.count() == 0


def test_import_rejects_tampered_package_with_zero_mutation(settings, signed_package):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    tampered_path = signed_package["path"].with_name("tampered.llpkg")
    tampered_path.write_bytes(_tamper_package_payload(signed_package["bytes"]))

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_node_package", "import", "--file", str(tampered_path))

    message = str(excinfo.value)
    assert "package import failed" in message
    assert "trust anchor" not in message
    assert InstalledPackage.objects.count() == 0


def test_import_rejects_malformed_deflate_member_with_zero_mutation(settings, signed_package):
    """Independent review Important I1, at the command layer: malformed
    compressed package content must flow
    verify_package -> PackageVerificationError -> import_package ->
    PackageImportError -> CommandError, never a raw zlib.error, and must
    never persist a partial/any InstalledPackage row."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    corrupted_path = signed_package["path"].with_name("corrupted.llpkg")
    corrupted_path.write_bytes(_corrupt_deflate_member(signed_package["bytes"], PAYLOAD_MEMBER))

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_node_package", "import", "--file", str(corrupted_path))

    message = str(excinfo.value)
    assert "zlib" not in message.lower()
    assert "package import failed" in message
    assert InstalledPackage.objects.count() == 0


def test_import_rejects_missing_package_file(settings, signed_package, tmp_path):
    """M1: file-read boundary after a successful trust load, on the
    import subcommand too (verify and import each read the file
    independently -- neither shares a cached result)."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    with pytest.raises(CommandError):
        call_command(
            "manage_node_package", "import", "--file", str(tmp_path / "does-not-exist.llpkg")
        )

    assert InstalledPackage.objects.count() == 0


def test_import_rejects_directory_as_package_file(settings, signed_package, tmp_path):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    a_directory = tmp_path / "a-directory"
    a_directory.mkdir()

    with pytest.raises(CommandError):
        call_command("manage_node_package", "import", "--file", str(a_directory))

    assert InstalledPackage.objects.count() == 0


def test_verify_then_tamper_then_import_fails(settings, signed_package):
    """Adversarial test (spec Section 16): a prior `verify` result is
    never a trusted cache. `import` always re-verifies the exact bytes
    on disk at the moment it runs."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

    signed_package["path"].write_bytes(_tamper_package_payload(signed_package["bytes"]))

    with pytest.raises(CommandError):
        call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

    assert InstalledPackage.objects.count() == 0


def test_import_fails_before_trust_loading_on_wrong_profile(
    settings, signed_package, monkeypatch
):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
    called = []
    monkeypatch.setattr(
        "linuxlab.sync.management.commands.manage_node_package.load_package_trusted_public_key",
        lambda: called.append(True),
    )

    with pytest.raises(CommandError):
        call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

    assert called == []


def test_import_rejects_wrong_profile_with_nonexistent_file_and_gives_capability_error(
    settings, monkeypatch
):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
    trust_called = []
    monkeypatch.setattr(
        "linuxlab.sync.management.commands.manage_node_package.load_package_trusted_public_key",
        lambda: trust_called.append(True),
    )

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_node_package", "import", "--file", "/definitely/does/not/exist.llpkg"
        )

    assert "not permitted on this deployment profile" in str(excinfo.value)
    assert trust_called == []
    assert InstalledPackage.objects.count() == 0


def test_importing_the_same_package_twice_is_idempotent(settings, signed_package, capsys):
    """import_package() itself is idempotent for identical bytes (Task 12:
    test_importing_the_same_package_twice_is_idempotent). The command must
    not invent different semantics of its own -- calling it twice must
    stage exactly one InstalledPackage row, not raise, and not duplicate."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    capsys.readouterr()
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

    out = capsys.readouterr().out
    assert "Operation: import" in out
    assert "State: staged" in out
    assert InstalledPackage.objects.count() == 1


# --- status -----------------------------------------------------------------


def test_status_with_no_args_lists_installed_packages_deterministically(
    settings, signed_package, capsys
):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    capsys.readouterr()

    call_command("manage_node_package", "status")

    out = capsys.readouterr().out
    assert f"Source: {signed_package['source_public_id']}" in out
    assert f"Version: {signed_package['version']}" in out
    assert "State: staged" in out


def test_status_rejects_wrong_profile_before_selector_validation(settings):
    """Capability-first (Task 4's established ordering, preserved here):
    malformed selector values must never even be parsed when the
    deployment profile cannot import packages."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_node_package",
            "status",
            "--source-public-id",
            "not-a-uuid",
            "--package-version",
            "not-a-version",
        )

    message = str(excinfo.value)
    assert "not permitted on this deployment profile" in message
    assert "not a valid UUID" not in message
    assert "version" not in message.lower()
    assert InstalledPackage.objects.count() == 0


def test_status_filters_by_exact_canonical_tuple(settings, signed_package, capsys):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    capsys.readouterr()

    call_command(
        "manage_node_package",
        "status",
        "--package-type",
        "learning_path",
        "--source-public-id",
        str(signed_package["source_public_id"]),
        "--package-version",
        str(signed_package["version"]),
    )

    out = capsys.readouterr().out
    assert f"Version: {signed_package['version']}" in out


def test_status_unknown_identity_returns_no_rows(settings, capsys):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command(
        "manage_node_package",
        "status",
        "--package-type",
        "learning_path",
        "--source-public-id",
        str(uuid.uuid4()),
        "--package-version",
        "1",
    )
    assert capsys.readouterr().out == ""
    assert InstalledPackage.objects.count() == 0


def test_status_rejects_version_without_source(settings):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    with pytest.raises(CommandError):
        call_command("manage_node_package", "status", "--package-version", "1")


def test_status_rejects_malformed_source_uuid(settings):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_node_package", "status", "--source-public-id", "not-a-uuid")

    assert "not a valid UUID" in str(excinfo.value)


def test_status_rejects_malformed_package_version(settings, signed_package):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_node_package",
            "status",
            "--source-public-id",
            str(signed_package["source_public_id"]),
            "--package-version",
            "not-a-version",
        )

    assert "not a valid version number" in str(excinfo.value)


def test_status_never_exposes_database_primary_key(settings, signed_package, capsys):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    capsys.readouterr()

    call_command("manage_node_package", "status")

    out = capsys.readouterr().out
    assert "pk" not in out.lower()
    assert "InstalledPackage object" not in out


def test_status_selects_exact_version_when_multiple_versions_exist(
    settings, tmp_path, monkeypatch, capsys
):
    """Load-bearing canonical-identity coverage (corrective Section 22):
    two real installed versions of the same source must never be confused
    -- querying one exact version reports exactly that version, never the
    other, and never a 'latest' substitute."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from linuxlab.activities.default_registry import build_default_activity_registry
    from linuxlab.content.models import LearningPathPage
    from linuxlab.publication.package import build_package
    from linuxlab.publication.snapshots import publish_learning_path

    from ._wagtail_bootstrap import get_or_create_root_page

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    root = get_or_create_root_page()
    path = LearningPathPage(title="Two Versions", slug=f"two-versions-{uuid.uuid4().hex[:8]}")
    root.add_child(instance=path)

    registry = build_default_activity_registry()
    private_key = Ed25519PrivateKey.generate()

    trust_path = tmp_path / "package-signing-public.key"
    trust_path.write_bytes(
        private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", trust_path)

    snapshot_v1 = publish_learning_path(path, registry)
    package_v1_path = tmp_path / "package-v1.llpkg"
    package_v1_path.write_bytes(build_package(snapshot_v1, registry, private_key))

    live_path = LearningPathPage.objects.get(public_id=snapshot_v1.source_public_id)
    snapshot_v2 = publish_learning_path(live_path, registry)
    package_v2_path = tmp_path / "package-v2.llpkg"
    package_v2_path.write_bytes(build_package(snapshot_v2, registry, private_key))

    call_command("manage_node_package", "import", "--file", str(package_v1_path))
    call_command("manage_node_package", "import", "--file", str(package_v2_path))
    capsys.readouterr()
    source_public_id = str(snapshot_v1.source_public_id)

    call_command(
        "manage_node_package", "status",
        "--source-public-id", source_public_id,
        "--package-version", "1",
    )
    out_v1 = capsys.readouterr().out
    assert "Version: 1" in out_v1
    assert "Version: 2" not in out_v1

    call_command(
        "manage_node_package", "status",
        "--source-public-id", source_public_id,
        "--package-version", "2",
    )
    out_v2 = capsys.readouterr().out
    assert "Version: 2" in out_v2
    assert "Version: 1" not in out_v2


def test_status_reflects_real_active_state_after_activation(settings, signed_package, capsys):
    """State authenticity (corrective Section 23): status must report the
    real current lifecycle state, not merely echo `staged` from import.
    activate_package() is used only to construct pre-existing state, as
    the corrective explicitly permits -- Task 5 exposes no activate CLI."""
    from linuxlab.sync.services import activate_package

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    installed = InstalledPackage.objects.get()
    activate_package(installed)
    capsys.readouterr()

    call_command("manage_node_package", "status")

    out = capsys.readouterr().out
    assert "State: active" in out
    assert "State: staged" not in out


def test_status_leaves_installed_package_inventory_unchanged(settings, signed_package):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    installed_before = InstalledPackage.objects.get()
    identity_before = (
        installed_before.pk,
        installed_before.package_type,
        installed_before.source_public_id,
        installed_before.package_version,
        installed_before.state,
        installed_before.previous_package_id,
    )

    call_command("manage_node_package", "status")

    assert InstalledPackage.objects.count() == 1
    installed_after = InstalledPackage.objects.get()
    identity_after = (
        installed_after.pk,
        installed_after.package_type,
        installed_after.source_public_id,
        installed_after.package_version,
        installed_after.state,
        installed_after.previous_package_id,
    )
    assert identity_after == identity_before


def test_status_does_not_load_trust_anchor(settings, signed_package, monkeypatch):
    """Trust separation (corrective Section 8/25/27): status is local DB
    inspection, never a trust operation. Proven by making the trust
    loader raise if it is ever called."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

    def _raise_if_called():
        raise AssertionError("status must not load the trust anchor")

    monkeypatch.setattr(
        "linuxlab.sync.management.commands.manage_node_package.load_package_trusted_public_key",
        _raise_if_called,
    )

    call_command("manage_node_package", "status")


def test_status_succeeds_with_no_trust_configuration_at_all(
    settings, signed_package, tmp_path, monkeypatch, capsys
):
    """Stronger trust-separation proof: status succeeds even when the
    trust anchor is completely missing/invalid, since it never needs
    trust material at all."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    capsys.readouterr()

    monkeypatch.setattr(
        "linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "does-not-exist.key"
    )

    call_command("manage_node_package", "status")

    out = capsys.readouterr().out
    assert "State: staged" in out


# --- activate -----------------------------------------------------------


def test_activate_transitions_staged_package_to_active(settings, signed_package, capsys):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    capsys.readouterr()

    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(signed_package["source_public_id"]),
        "--package-version", str(signed_package["version"]),
    )

    out = capsys.readouterr().out
    assert "Operation: activate" in out
    assert "State: active" in out


def test_activate_rejects_unknown_tuple(settings):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    with pytest.raises(CommandError):
        call_command(
            "manage_node_package", "activate",
            "--package-type", "learning_path",
            "--source-public-id", str(uuid.uuid4()),
            "--package-version", "1",
        )
    assert InstalledPackage.objects.count() == 0


def test_activate_rejects_malformed_source_uuid(settings):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_node_package", "activate",
            "--package-type", "learning_path",
            "--source-public-id", "not-a-uuid",
            "--package-version", "1",
        )
    assert "not a valid UUID" in str(excinfo.value)


def test_activate_rejects_malformed_version(settings, signed_package):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_node_package", "activate",
            "--package-type", "learning_path",
            "--source-public-id", str(signed_package["source_public_id"]),
            "--package-version", "not-a-version",
        )
    assert "not a valid version number" in str(excinfo.value)


def test_activate_rejects_wrong_profile_before_selector_validation(settings):
    """Capability-first (corrective Section 17): a malformed UUID/version
    combined with capability off must still surface only the capability
    rejection, proving selector parsing never runs first."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_node_package", "activate",
            "--package-type", "learning_path",
            "--source-public-id", "not-a-uuid",
            "--package-version", "not-a-version",
        )

    message = str(excinfo.value)
    assert "not permitted on this deployment profile" in message
    assert "not a valid UUID" not in message
    assert "version" not in message.lower()
    assert InstalledPackage.objects.count() == 0


def test_activate_fails_before_mutation_on_wrong_profile(settings, signed_package):
    from linuxlab.sync.models import ACTIVE

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
    with pytest.raises(CommandError):
        call_command(
            "manage_node_package", "activate",
            "--package-type", "learning_path",
            "--source-public-id", str(signed_package["source_public_id"]),
            "--package-version", str(signed_package["version"]),
        )

    assert not InstalledPackage.objects.filter(state=ACTIVE).exists()


def test_activate_maintains_single_active_invariant(settings, signed_package):
    from linuxlab.sync.models import ACTIVE

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(signed_package["source_public_id"]),
        "--package-version", str(signed_package["version"]),
    )

    active_count = InstalledPackage.objects.filter(
        package_type="learning_path",
        source_public_id=signed_package["source_public_id"],
        state=ACTIVE,
    ).count()
    assert active_count == 1


def test_activate_selects_exact_version_when_multiple_versions_exist(
    settings, tmp_path, monkeypatch, capsys
):
    """Load-bearing canonical-identity coverage (corrective Section 21):
    two real installed versions of the same source; activating version 1
    must activate exactly version 1, never version 2, and vice versa."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from linuxlab.activities.default_registry import build_default_activity_registry
    from linuxlab.content.models import LearningPathPage
    from linuxlab.publication.package import build_package
    from linuxlab.publication.snapshots import publish_learning_path

    from ._wagtail_bootstrap import get_or_create_root_page

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    root = get_or_create_root_page()
    path = LearningPathPage(
        title="Activate Two Versions", slug=f"activate-two-versions-{uuid.uuid4().hex[:8]}"
    )
    root.add_child(instance=path)

    registry = build_default_activity_registry()
    private_key = Ed25519PrivateKey.generate()

    trust_path = tmp_path / "package-signing-public.key"
    trust_path.write_bytes(
        private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", trust_path)

    snapshot_v1 = publish_learning_path(path, registry)
    package_v1_path = tmp_path / "package-v1.llpkg"
    package_v1_path.write_bytes(build_package(snapshot_v1, registry, private_key))

    live_path = LearningPathPage.objects.get(public_id=snapshot_v1.source_public_id)
    snapshot_v2 = publish_learning_path(live_path, registry)
    package_v2_path = tmp_path / "package-v2.llpkg"
    package_v2_path.write_bytes(build_package(snapshot_v2, registry, private_key))

    call_command("manage_node_package", "import", "--file", str(package_v1_path))
    call_command("manage_node_package", "import", "--file", str(package_v2_path))
    source_public_id = str(snapshot_v1.source_public_id)

    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", source_public_id,
        "--package-version", "1",
    )
    capsys.readouterr()

    from linuxlab.sync.models import ACTIVE

    active = InstalledPackage.objects.get(
        package_type="learning_path", source_public_id=snapshot_v1.source_public_id, state=ACTIVE
    )
    assert active.package_version == 1

    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", source_public_id,
        "--package-version", "2",
    )
    out = capsys.readouterr().out
    assert "Version: 2" in out
    assert "State: active" in out

    from linuxlab.sync.models import ARCHIVED

    active = InstalledPackage.objects.get(
        package_type="learning_path", source_public_id=snapshot_v1.source_public_id, state=ACTIVE
    )
    assert active.package_version == 2
    # Service-owned previous_package semantics (Task 12): activating
    # version 2 archives version 1 and records it as version 2's
    # previous_package -- asserting the actual existing contract, not a
    # guessed transition.
    assert active.previous_package.package_version == 1
    assert active.previous_package.state == ARCHIVED


def test_activate_does_not_load_trust_anchor(settings, signed_package, monkeypatch):
    """Trust separation (corrective Section 8/32): activate is a local
    lifecycle operation on already-installed state, never a trust
    operation."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

    def _raise_if_called():
        raise AssertionError("activate must not load the trust anchor")

    monkeypatch.setattr(
        "linuxlab.sync.management.commands.manage_node_package.load_package_trusted_public_key",
        _raise_if_called,
    )

    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(signed_package["source_public_id"]),
        "--package-version", str(signed_package["version"]),
    )


# --- rollback -------------------------------------------------------------


def test_rollback_reactivates_previous_package(settings, rollback_world, capsys):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    world = rollback_world

    call_command("manage_node_package", "import", "--file", str(world["path"]))
    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
        "--package-version", str(world["version"]),
    )
    call_command("manage_node_package", "import", "--file", str(world["path_b"]))
    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
        "--package-version", str(world["version_b"]),
    )
    capsys.readouterr()

    call_command(
        "manage_node_package", "rollback",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
    )

    out = capsys.readouterr().out
    assert f"Version: {world['version']}" in out
    assert "State: active" in out


def test_rollback_fails_with_no_active_package(settings):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    with pytest.raises(CommandError):
        call_command(
            "manage_node_package", "rollback",
            "--package-type", "learning_path",
            "--source-public-id", str(uuid.uuid4()),
        )


def test_rollback_fails_when_active_has_no_previous_package(settings, signed_package):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(signed_package["source_public_id"]),
        "--package-version", str(signed_package["version"]),
    )

    with pytest.raises(CommandError):
        call_command(
            "manage_node_package", "rollback",
            "--package-type", "learning_path",
            "--source-public-id", str(signed_package["source_public_id"]),
        )

    assert (
        InstalledPackage.objects.get(source_public_id=signed_package["source_public_id"]).state
        == "active"
    )


def test_rollback_rejects_malformed_source_uuid(settings):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_node_package", "rollback",
            "--package-type", "learning_path", "--source-public-id", "not-a-uuid",
        )
    assert "not a valid UUID" in str(excinfo.value)


def test_rollback_rejects_wrong_profile_before_selector_validation(settings):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_node_package", "rollback",
            "--package-type", "learning_path", "--source-public-id", "not-a-uuid",
        )

    message = str(excinfo.value)
    assert "not permitted on this deployment profile" in message
    assert "not a valid UUID" not in message


def test_rollback_fails_before_mutation_on_wrong_profile(settings, rollback_world):
    from linuxlab.sync.models import ACTIVE

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    world = rollback_world
    call_command("manage_node_package", "import", "--file", str(world["path"]))
    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
        "--package-version", str(world["version"]),
    )
    call_command("manage_node_package", "import", "--file", str(world["path_b"]))
    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
        "--package-version", str(world["version_b"]),
    )

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
    with pytest.raises(CommandError):
        call_command(
            "manage_node_package", "rollback",
            "--package-type", "learning_path",
            "--source-public-id", str(world["source_public_id"]),
        )

    active = InstalledPackage.objects.get(
        package_type="learning_path", source_public_id=world["source_public_id"], state=ACTIVE
    )
    assert active.package_version == world["version_b"]


def test_rollback_maintains_single_active_invariant(settings, rollback_world):
    from linuxlab.sync.models import ACTIVE

    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    world = rollback_world
    call_command("manage_node_package", "import", "--file", str(world["path"]))
    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
        "--package-version", str(world["version"]),
    )
    call_command("manage_node_package", "import", "--file", str(world["path_b"]))
    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
        "--package-version", str(world["version_b"]),
    )
    call_command(
        "manage_node_package", "rollback",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
    )

    active_count = InstalledPackage.objects.filter(
        package_type="learning_path",
        source_public_id=world["source_public_id"],
        state=ACTIVE,
    ).count()
    assert active_count == 1


def test_rollback_does_not_load_trust_anchor(settings, rollback_world, monkeypatch):
    """Trust separation (corrective Section 8/32): rollback acts purely on
    already-installed lifecycle state."""
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    world = rollback_world
    call_command("manage_node_package", "import", "--file", str(world["path"]))
    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
        "--package-version", str(world["version"]),
    )
    call_command("manage_node_package", "import", "--file", str(world["path_b"]))
    call_command(
        "manage_node_package", "activate",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
        "--package-version", str(world["version_b"]),
    )

    def _raise_if_called():
        raise AssertionError("rollback must not load the trust anchor")

    monkeypatch.setattr(
        "linuxlab.sync.management.commands.manage_node_package.load_package_trusted_public_key",
        _raise_if_called,
    )

    call_command(
        "manage_node_package", "rollback",
        "--package-type", "learning_path",
        "--source-public-id", str(world["source_public_id"]),
    )
