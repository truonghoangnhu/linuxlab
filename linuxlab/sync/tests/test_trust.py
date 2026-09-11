import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from linuxlab.sync.trust import PackageTrustConfigurationError, load_package_trusted_public_key


def test_valid_ed25519_pem_returns_public_key(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key_path = tmp_path / "package-signing-public.key"
    public_key = Ed25519PrivateKey.generate().public_key()
    key_path.write_bytes(
        public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)

    loaded = load_package_trusted_public_key()

    assert isinstance(loaded, Ed25519PublicKey)


def test_missing_path_raises_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "does-not-exist.key"
    )
    with pytest.raises(PackageTrustConfigurationError):
        load_package_trusted_public_key()


def test_directory_path_raises_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path)
    with pytest.raises(PackageTrustConfigurationError):
        load_package_trusted_public_key()


def test_empty_file_raises_configuration_error(tmp_path, monkeypatch):
    key_path = tmp_path / "empty.key"
    key_path.write_bytes(b"")
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
    with pytest.raises(PackageTrustConfigurationError):
        load_package_trusted_public_key()


def test_malformed_pem_raises_configuration_error(tmp_path, monkeypatch):
    key_path = tmp_path / "malformed.key"
    key_path.write_bytes(b"-----BEGIN PUBLIC KEY-----\nnot valid\n")
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
    with pytest.raises(PackageTrustConfigurationError):
        load_package_trusted_public_key()


def test_rsa_public_key_raises_configuration_error(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key_path = tmp_path / "rsa.key"
    rsa_public = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    key_path.write_bytes(rsa_public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
    with pytest.raises(PackageTrustConfigurationError):
        load_package_trusted_public_key()


def test_ecdsa_public_key_raises_configuration_error(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key_path = tmp_path / "ecdsa.key"
    ec_public = ec.generate_private_key(ec.SECP256R1()).public_key()
    key_path.write_bytes(ec_public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
    with pytest.raises(PackageTrustConfigurationError):
        load_package_trusted_public_key()


def test_error_message_never_contains_pem_bytes(tmp_path, monkeypatch):
    key_path = tmp_path / "malformed.key"
    sentinel = b"TEST_TRUST_ANCHOR_SENTINEL_BYTES"
    key_path.write_bytes(sentinel)
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
    with pytest.raises(PackageTrustConfigurationError) as excinfo:
        load_package_trusted_public_key()
    assert sentinel.decode() not in str(excinfo.value)


def test_symlink_source_raises_configuration_error(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    real_key_path = tmp_path / "real-package-signing-public.key"
    public_key = Ed25519PrivateKey.generate().public_key()
    real_key_path.write_bytes(
        public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )

    symlink_path = tmp_path / "package-signing-public.key"
    symlink_path.symlink_to(real_key_path)
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", symlink_path)

    with pytest.raises(PackageTrustConfigurationError):
        load_package_trusted_public_key()


def test_fifo_source_raises_configuration_error_without_hanging(tmp_path, monkeypatch):
    import os
    import threading

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    fifo_path = tmp_path / "package-signing-public.key"
    os.mkfifo(fifo_path)
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", fifo_path)

    valid_pem = Ed25519PrivateKey.generate().public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )

    def _write_once_a_reader_appears():
        # A blocking writer-open on a FIFO only returns once some process
        # has the read end open, so reaching this write proves the FIFO
        # genuinely had valid PEM bytes available to a reader -- this
        # test exercises real rejection of a readable special file, not
        # a fast failure caused by an empty/unconnected pipe. The correct
        # (fast, non-blocking, fstat-rejecting) implementation may have
        # already closed its read end by the time the write is attempted
        # -- that race is expected and fine; this thread only needs to
        # prove the blocking open unblocked, not that the write lands.
        fd = os.open(fifo_path, os.O_WRONLY)
        try:
            try:
                os.write(fd, valid_pem)
            except OSError:
                pass
        finally:
            os.close(fd)

    writer = threading.Thread(target=_write_once_a_reader_appears, daemon=True)
    writer.start()

    result = {}

    def _call_loader():
        try:
            load_package_trusted_public_key()
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread below
            result["exc"] = exc

    caller = threading.Thread(target=_call_loader, daemon=True)
    caller.start()
    caller.join(timeout=5)
    writer.join(timeout=5)

    assert not caller.is_alive(), "load_package_trusted_public_key() hung on a FIFO source"
    assert isinstance(result.get("exc"), PackageTrustConfigurationError)


def test_open_permission_error_raises_configuration_error(tmp_path, monkeypatch):
    import os

    key_path = tmp_path / "package-signing-public.key"
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)

    def _raise_permission_error(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "open", _raise_permission_error)

    with pytest.raises(PackageTrustConfigurationError):
        load_package_trusted_public_key()


def test_loaded_public_key_bytes_equal_written_bytes(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key_path = tmp_path / "package-signing-public.key"
    public_key = Ed25519PrivateKey.generate().public_key()
    written_bytes = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    key_path.write_bytes(written_bytes)
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)

    loaded = load_package_trusted_public_key()

    loaded_bytes = loaded.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    assert loaded_bytes == written_bytes
