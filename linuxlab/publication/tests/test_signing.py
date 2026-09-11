import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from linuxlab.publication.signing import (
    SIGNATURE_ALGORITHM,
    PackageSigningConfigurationError,
    load_package_signing_private_key,
    sign_bytes,
    verify_signature,
)


def test_signature_algorithm_is_ed25519():
    assert SIGNATURE_ALGORITHM == "ed25519"


def test_a_valid_signature_verifies_against_the_matching_public_key():
    private_key = Ed25519PrivateKey.generate()
    message = b"canonical manifest and payload bytes"

    signature = sign_bytes(message, private_key)

    verify_signature(message, signature, private_key.public_key())  # must not raise


def test_signature_is_64_bytes():
    private_key = Ed25519PrivateKey.generate()

    signature = sign_bytes(b"some message", private_key)

    assert len(signature) == 64


def test_verification_fails_against_a_different_keypairs_public_key():
    signer_key = Ed25519PrivateKey.generate()
    other_key = Ed25519PrivateKey.generate()
    message = b"canonical manifest and payload bytes"

    signature = sign_bytes(message, signer_key)

    with pytest.raises(InvalidSignature):
        verify_signature(message, signature, other_key.public_key())


def test_verification_fails_for_a_modified_message():
    private_key = Ed25519PrivateKey.generate()
    message = b"canonical manifest and payload bytes"
    signature = sign_bytes(message, private_key)

    with pytest.raises(InvalidSignature):
        verify_signature(b"canonical manifest and payload BYTES", signature, private_key.public_key())


def test_verification_fails_for_a_flipped_signature_byte():
    private_key = Ed25519PrivateKey.generate()
    message = b"canonical manifest and payload bytes"
    signature = bytearray(sign_bytes(message, private_key))
    signature[0] ^= 0xFF

    with pytest.raises(InvalidSignature):
        verify_signature(message, bytes(signature), private_key.public_key())


def test_load_package_signing_private_key_returns_private_key(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    key_path = tmp_path / "package-signing.key"
    private_key = Ed25519PrivateKey.generate()
    key_path.write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)

    loaded = load_package_signing_private_key()

    assert isinstance(loaded, Ed25519PrivateKey)


def test_load_package_signing_private_key_missing_path_raises_configuration_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "linuxlab.publication.signing._SIGNING_KEY_PATH", tmp_path / "does-not-exist.key"
    )
    with pytest.raises(PackageSigningConfigurationError):
        load_package_signing_private_key()


def test_load_package_signing_private_key_directory_path_raises_configuration_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", tmp_path)
    with pytest.raises(PackageSigningConfigurationError):
        load_package_signing_private_key()


def test_load_package_signing_private_key_empty_file_raises_configuration_error(
    tmp_path, monkeypatch
):
    key_path = tmp_path / "empty.key"
    key_path.write_bytes(b"")
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)
    with pytest.raises(PackageSigningConfigurationError):
        load_package_signing_private_key()


def test_load_package_signing_private_key_malformed_pem_raises_configuration_error(
    tmp_path, monkeypatch
):
    key_path = tmp_path / "malformed.key"
    key_path.write_bytes(b"-----BEGIN PRIVATE KEY-----\nnot valid\n")
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)
    with pytest.raises(PackageSigningConfigurationError):
        load_package_signing_private_key()


def test_load_package_signing_private_key_rejects_rsa_private_key(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    key_path = tmp_path / "rsa.key"
    rsa_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(
        rsa_private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)
    with pytest.raises(PackageSigningConfigurationError):
        load_package_signing_private_key()


def test_load_package_signing_private_key_rejects_ecdsa_private_key(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    key_path = tmp_path / "ecdsa.key"
    ec_private = ec.generate_private_key(ec.SECP256R1())
    key_path.write_bytes(
        ec_private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)
    with pytest.raises(PackageSigningConfigurationError):
        load_package_signing_private_key()


def test_load_package_signing_private_key_error_message_never_contains_pem_bytes(
    tmp_path, monkeypatch
):
    key_path = tmp_path / "malformed.key"
    sentinel = b"TEST_PRIVATE_KEY_SENTINEL_BYTES"
    key_path.write_bytes(sentinel)
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)
    with pytest.raises(PackageSigningConfigurationError) as excinfo:
        load_package_signing_private_key()
    assert sentinel.decode() not in str(excinfo.value)


def test_load_package_signing_private_key_symlink_source_raises_configuration_error(
    tmp_path, monkeypatch
):
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    real_key_path = tmp_path / "real-package-signing.key"
    private_key = Ed25519PrivateKey.generate()
    real_key_path.write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )

    symlink_path = tmp_path / "package-signing.key"
    symlink_path.symlink_to(real_key_path)
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", symlink_path)

    with pytest.raises(PackageSigningConfigurationError):
        load_package_signing_private_key()


def test_load_package_signing_private_key_fifo_source_raises_without_hanging(
    tmp_path, monkeypatch
):
    import os
    import threading

    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    fifo_path = tmp_path / "package-signing.key"
    os.mkfifo(fifo_path)
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", fifo_path)

    valid_pem = Ed25519PrivateKey.generate().private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )

    def _write_once_a_reader_appears():
        # See the mirrored Node test (linuxlab/sync/tests/test_trust.py)
        # for the full race rationale: a blocking writer-open on a FIFO
        # only unblocks once a reader exists, which is what this proves;
        # the correct fast-rejecting implementation may already have
        # closed its read end by the time the write is attempted, which
        # is an expected, harmless race, not a test bug.
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
            load_package_signing_private_key()
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread below
            result["exc"] = exc

    caller = threading.Thread(target=_call_loader, daemon=True)
    caller.start()
    caller.join(timeout=5)
    writer.join(timeout=5)

    assert not caller.is_alive(), "load_package_signing_private_key() hung on a FIFO source"
    assert isinstance(result.get("exc"), PackageSigningConfigurationError)


def test_load_package_signing_private_key_open_permission_error_raises_configuration_error(
    tmp_path, monkeypatch
):
    import os

    key_path = tmp_path / "package-signing.key"
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)

    def _raise_permission_error(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "open", _raise_permission_error)

    with pytest.raises(PackageSigningConfigurationError):
        load_package_signing_private_key()


def test_load_package_signing_private_key_derived_public_bytes_match_generated_key(
    tmp_path, monkeypatch
):
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    key_path = tmp_path / "package-signing.key"
    private_key = Ed25519PrivateKey.generate()
    key_path.write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)

    loaded = load_package_signing_private_key()

    expected_public_bytes = private_key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    loaded_public_bytes = loaded.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    assert loaded_public_bytes == expected_public_bytes


def test_load_package_signing_private_key_rejects_encrypted_pkcs8(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.serialization import (
        BestAvailableEncryption,
        Encoding,
        PrivateFormat,
    )

    key_path = tmp_path / "package-signing.key"
    private_key = Ed25519PrivateKey.generate()
    key_path.write_bytes(
        private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, BestAvailableEncryption(b"a-test-password")
        )
    )
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)

    with pytest.raises(PackageSigningConfigurationError):
        load_package_signing_private_key()
