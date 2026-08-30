import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from linuxlab.publication.signing import SIGNATURE_ALGORITHM, sign_bytes, verify_signature


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
