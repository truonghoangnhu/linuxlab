import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.models import PublicationSnapshot

from ._wagtail_bootstrap import get_or_create_root_page


def _make_learning_path(title="Intro to Bash"):
    root = get_or_create_root_page()
    path = LearningPathPage(title=title, slug=title.lower().replace(" ", "-"))
    root.add_child(instance=path)
    return path


@pytest.mark.django_db(transaction=True)
def test_publish_capability_guard_precedes_uuid_validation(settings):
    settings.LINUXLAB_CAN_AUTHOR = False

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_central_package", "publish", "--learning-path", "not-a-uuid")

    message = str(excinfo.value)
    assert "not permitted on this deployment profile" in message
    assert "not a valid UUID" not in message
    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_publish_rejects_malformed_uuid(settings):
    settings.LINUXLAB_CAN_AUTHOR = True

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_central_package", "publish", "--learning-path", "not-a-uuid")

    message = str(excinfo.value)
    assert "not a valid UUID" in message
    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_publish_rejects_unknown_learning_path(settings):
    settings.LINUXLAB_CAN_AUTHOR = True
    unknown_id = uuid.uuid4()

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_central_package", "publish", "--learning-path", str(unknown_id))

    message = str(excinfo.value)
    assert str(unknown_id) in message
    assert "LearningPathPage object" not in message
    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_publish_reports_kind_learning_path_and_version(settings, capsys):
    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path()

    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

    snapshot = PublicationSnapshot.objects.get(source_public_id=path.public_id)
    assert snapshot.kind == "learning_path"
    assert snapshot.source_public_id == path.public_id
    assert snapshot.version == 1

    out = capsys.readouterr().out
    assert out == (
        "Operation: publish\n"
        "Kind: learning_path\n"
        f"LearningPath: {path.public_id}\n"
        "Version: 1\n"
    )
    assert "Publication:" not in out


@pytest.mark.django_db(transaction=True)
def test_publish_translates_existing_domain_rejection_for_unresolved_activity(settings):
    # Real, already-existing domain rejection (linuxlab/publication/
    # validators.py::_validate_activity_reference): an activity block
    # referencing an activity_public_id with no published ActivityVersion
    # raises ValueError from inside publish_learning_path() itself. This
    # is not a new CLI-invented failure mode -- it is the same rejection
    # linuxlab/publication/tests/test_publish_learning_path.py's
    # test_publish_rejected_when_activity_reference_unpublished already
    # exercises directly against the service.
    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path(title="Broken Path")
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(uuid.uuid4()),
                "activity_version": 1,
            },
        )
    ]
    module.add_child(instance=lesson)

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

    assert "Traceback" not in str(excinfo.value)
    assert PublicationSnapshot.objects.filter(source_public_id=path.public_id).count() == 0


@pytest.mark.django_db(transaction=True)
def test_publish_rejects_wrong_profile_before_mutation_with_valid_target(settings):
    settings.LINUXLAB_CAN_AUTHOR = False
    path = _make_learning_path()

    with pytest.raises(CommandError):
        call_command(
            "manage_central_package", "publish", "--learning-path", str(path.public_id)
        )

    assert not PublicationSnapshot.objects.filter(source_public_id=path.public_id).exists()


@pytest.mark.django_db(transaction=True)
def test_publish_second_call_allocates_next_version(settings, capsys):
    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path()

    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))
    capsys.readouterr()
    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

    out = capsys.readouterr().out
    assert "Version: 2" in out


def test_write_new_file_atomically_creates_file_with_exact_bytes(tmp_path):
    from linuxlab.publication.management.commands.manage_central_package import (
        _write_new_file_atomically,
    )

    destination = tmp_path / "out.llpkg"
    _write_new_file_atomically(destination, b"package-bytes")

    assert destination.read_bytes() == b"package-bytes"


def test_write_new_file_atomically_refuses_existing_destination(tmp_path):
    from linuxlab.publication.management.commands.manage_central_package import (
        _write_new_file_atomically,
    )

    destination = tmp_path / "out.llpkg"
    destination.write_bytes(b"original")

    with pytest.raises(CommandError):
        _write_new_file_atomically(destination, b"new-bytes")

    assert destination.read_bytes() == b"original"


def test_write_new_file_atomically_leaves_no_temp_file_on_existing_destination(tmp_path):
    from linuxlab.publication.management.commands.manage_central_package import (
        _write_new_file_atomically,
    )

    destination = tmp_path / "out.llpkg"
    destination.write_bytes(b"original")

    with pytest.raises(CommandError):
        _write_new_file_atomically(destination, b"new-bytes")

    remaining = list(tmp_path.iterdir())
    assert remaining == [destination]


def test_write_new_file_atomically_fails_closed_on_race_after_temp_write(tmp_path, monkeypatch):
    """Proves the FINAL authority is the atomic os.link() call itself, not
    any pre-check: a competing file is created concurrently, AFTER our temp
    bytes are already fully written, but immediately BEFORE our own
    os.link() call runs. os.link() must still fail closed (EEXIST) rather
    than silently overwriting the competing content."""
    import os as os_module

    from linuxlab.publication.management.commands.manage_central_package import (
        _write_new_file_atomically,
    )

    destination = tmp_path / "out.llpkg"
    competing_bytes = b"COMPETING_BYTES_FROM_CONCURRENT_WRITER"
    real_link = os_module.link

    def _create_destination_then_link(*args, **kwargs):
        destination.write_bytes(competing_bytes)
        return real_link(*args, **kwargs)

    monkeypatch.setattr(os_module, "link", _create_destination_then_link)

    with pytest.raises(CommandError):
        _write_new_file_atomically(destination, b"our-bytes")

    assert destination.read_bytes() == competing_bytes
    assert list(tmp_path.iterdir()) == [destination]


def test_write_new_file_atomically_translates_write_failure_and_cleans_up(tmp_path, monkeypatch):
    import os as os_module

    from linuxlab.publication.management.commands.manage_central_package import (
        _write_new_file_atomically,
    )

    destination = tmp_path / "out.llpkg"
    real_fdopen = os_module.fdopen

    class _FailingWriteHandle:
        def __init__(self, real_handle):
            self._real_handle = real_handle

        def write(self, *args, **kwargs):
            raise OSError("TEST_WRITE_FAILURE")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self._real_handle.close()
            return False

    def _wrap_real_fdopen(fd, *args, **kwargs):
        return _FailingWriteHandle(real_fdopen(fd, *args, **kwargs))

    monkeypatch.setattr(os_module, "fdopen", _wrap_real_fdopen)

    with pytest.raises(CommandError) as excinfo:
        _write_new_file_atomically(destination, b"our-bytes")

    assert "TEST_WRITE_FAILURE" not in str(excinfo.value)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_new_file_atomically_translates_fsync_failure_and_cleans_up(tmp_path, monkeypatch):
    import os as os_module

    from linuxlab.publication.management.commands.manage_central_package import (
        _write_new_file_atomically,
    )

    destination = tmp_path / "out.llpkg"

    def _raise_fsync_failure(*args, **kwargs):
        raise OSError("TEST_FSYNC_FAILURE")

    monkeypatch.setattr(os_module, "fsync", _raise_fsync_failure)

    with pytest.raises(CommandError) as excinfo:
        _write_new_file_atomically(destination, b"our-bytes")

    assert "TEST_FSYNC_FAILURE" not in str(excinfo.value)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_new_file_atomically_translates_link_failure_other_than_eexist(tmp_path, monkeypatch):
    import os as os_module

    from linuxlab.publication.management.commands.manage_central_package import (
        _write_new_file_atomically,
    )

    destination = tmp_path / "out.llpkg"

    def _raise_link_failure(*args, **kwargs):
        raise OSError("TEST_LINK_FAILURE")

    monkeypatch.setattr(os_module, "link", _raise_link_failure)

    with pytest.raises(CommandError) as excinfo:
        _write_new_file_atomically(destination, b"our-bytes")

    assert "TEST_LINK_FAILURE" not in str(excinfo.value)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.django_db(transaction=True)
def test_build_capability_guard_precedes_uuid_and_version_validation(settings, tmp_path):
    settings.LINUXLAB_CAN_AUTHOR = False
    output_path = tmp_path / "out.llpkg"

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_central_package", "build",
            "--learning-path", "not-a-uuid",
            "--version", "invalid-version",
            "--output", str(output_path),
        )

    message = str(excinfo.value)
    assert "not permitted on this deployment profile" in message
    assert "not a valid UUID" not in message
    assert "version" not in message.lower()
    assert not output_path.exists()
    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_build_rejects_malformed_learning_path_uuid(settings, tmp_path):
    settings.LINUXLAB_CAN_AUTHOR = True
    output_path = tmp_path / "out.llpkg"

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_central_package", "build",
            "--learning-path", "not-a-uuid", "--version", "1", "--output", str(output_path),
        )

    assert "not a valid UUID" in str(excinfo.value)
    assert not output_path.exists()


@pytest.mark.django_db(transaction=True)
def test_build_rejects_invalid_version(settings, tmp_path):
    settings.LINUXLAB_CAN_AUTHOR = True
    output_path = tmp_path / "out.llpkg"

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_central_package", "build",
            "--learning-path", str(uuid.uuid4()),
            "--version", "not-a-version",
            "--output", str(output_path),
        )

    assert "version" in str(excinfo.value).lower()
    assert not output_path.exists()
    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_build_rejects_unknown_learning_path(settings, tmp_path):
    settings.LINUXLAB_CAN_AUTHOR = True
    output_path = tmp_path / "out.llpkg"

    with pytest.raises(CommandError):
        call_command(
            "manage_central_package", "build",
            "--learning-path", str(uuid.uuid4()), "--version", "1",
            "--output", str(output_path),
        )

    assert not output_path.exists()


@pytest.mark.django_db(transaction=True)
def test_build_rejects_nonexistent_version_does_not_select_a_different_one(settings, tmp_path):
    # Adversarial shape (coordinator Section 14): version 1 exists;
    # requesting version 2 must fail, never silently build version 1.
    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path()
    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))
    output_path = tmp_path / "out.llpkg"

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_central_package", "build",
            "--learning-path", str(path.public_id), "--version", "2",
            "--output", str(output_path),
        )

    assert str(path.public_id) in str(excinfo.value)
    assert not output_path.exists()


@pytest.mark.django_db(transaction=True)
def test_build_rejects_zero_version(settings, tmp_path):
    settings.LINUXLAB_CAN_AUTHOR = True
    output_path = tmp_path / "out.llpkg"

    with pytest.raises(CommandError):
        call_command(
            "manage_central_package", "build",
            "--learning-path", str(uuid.uuid4()),
            "--version", "0",
            "--output", str(output_path),
        )

    assert not output_path.exists()


@pytest.mark.django_db(transaction=True)
def test_build_fails_closed_on_missing_signing_configuration(settings, tmp_path, monkeypatch):
    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path()
    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

    monkeypatch.setattr(
        "linuxlab.publication.signing._SIGNING_KEY_PATH", tmp_path / "missing.key"
    )
    output_path = tmp_path / "out.llpkg"

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "manage_central_package", "build",
            "--learning-path", str(path.public_id), "--version", "1",
            "--output", str(output_path),
        )

    assert not output_path.exists()
    assert "BEGIN" not in str(excinfo.value)
    assert "PRIVATE KEY" not in str(excinfo.value)


@pytest.mark.django_db(transaction=True)
def test_build_produces_verifiable_package(settings, tmp_path, monkeypatch, capsys):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    from linuxlab.activities.default_registry import build_default_activity_registry
    from linuxlab.publication.package import verify_package

    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path()
    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))
    capsys.readouterr()

    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "package-signing.key"
    key_path.write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)

    output_path = tmp_path / "out.llpkg"
    call_command(
        "manage_central_package", "build",
        "--learning-path", str(path.public_id),
        "--version", "1",
        "--output", str(output_path),
    )

    assert output_path.exists()

    verified = verify_package(
        output_path.read_bytes(), build_default_activity_registry(), private_key.public_key()
    )
    assert str(verified.manifest.package_uuid) == str(path.public_id)
    assert verified.manifest.package_type == "learning_path"
    assert verified.manifest.package_version == 1

    assert PublicationSnapshot.objects.filter(source_public_id=path.public_id).count() == 1

    out = capsys.readouterr().out
    assert out == (
        "Operation: build\n"
        "Kind: learning_path\n"
        f"LearningPath: {path.public_id}\n"
        "Version: 1\n"
        "PackageType: learning_path\n"
        "PackageVersion: 1\n"
        f"PackageUUID: {path.public_id}\n"
        f"Output: {output_path}\n"
    )


@pytest.mark.django_db(transaction=True)
def test_build_fails_before_file_work_on_wrong_profile(settings, tmp_path):
    settings.LINUXLAB_CAN_AUTHOR = False
    output_path = tmp_path / "out.llpkg"

    with pytest.raises(CommandError):
        call_command(
            "manage_central_package", "build",
            "--learning-path", str(uuid.uuid4()), "--version", "1",
            "--output", str(output_path),
        )

    assert not output_path.exists()


@pytest.mark.django_db(transaction=True)
def test_build_command_level_existing_destination_preserved_unchanged(
    settings, tmp_path, monkeypatch
):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path()
    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "package-signing.key"
    key_path.write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)

    output_path = tmp_path / "out.llpkg"
    sentinel_bytes = b"ORIGINAL_DESTINATION_BYTES"
    output_path.write_bytes(sentinel_bytes)

    with pytest.raises(CommandError):
        call_command(
            "manage_central_package", "build",
            "--learning-path", str(path.public_id), "--version", "1",
            "--output", str(output_path),
        )

    assert output_path.read_bytes() == sentinel_bytes
    remaining = [entry.name for entry in tmp_path.iterdir()]
    assert sorted(remaining) == sorted(["package-signing.key", "out.llpkg"])


@pytest.mark.django_db(transaction=True)
def test_build_rejects_invalid_output_parent(settings, tmp_path):
    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path()
    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

    with pytest.raises(CommandError):
        call_command(
            "manage_central_package", "build",
            "--learning-path", str(path.public_id), "--version", "1",
            "--output", str(tmp_path / "does-not-exist" / "out.llpkg"),
        )
