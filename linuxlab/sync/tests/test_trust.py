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
