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


# --- verify -------------------------------------------------------------


def test_verify_succeeds_on_valid_package(settings, signed_package, capsys):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

    out = capsys.readouterr().out
    assert "Operation: verify" in out
    assert f"Source: {signed_package['source_public_id']}" in out


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

    with pytest.raises(CommandError):
        call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

    assert InstalledPackage.objects.count() == 0


def test_verify_rejects_tampered_package(settings, signed_package):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    tampered_path = signed_package["path"].with_name("tampered.llpkg")
    tampered_path.write_bytes(_tamper_package_payload(signed_package["bytes"]))

    with pytest.raises(CommandError):
        call_command("manage_node_package", "verify", "--file", str(tampered_path))

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
    assert "Operation: import" in out
    assert f"Source: {signed_package['source_public_id']}" in out
    assert f"Version: {signed_package['version']}" in out
    assert "State: staged" in out


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

    with pytest.raises(CommandError):
        call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

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

    with pytest.raises(CommandError):
        call_command("manage_node_package", "import", "--file", str(tampered_path))

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
