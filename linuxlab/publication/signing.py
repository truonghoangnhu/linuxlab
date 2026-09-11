"""Ed25519 signing primitives for .llpkg packages (ADR-010, Task 11).

A thin wrapper around the `cryptography` package's own Ed25519
implementation -- never a hand-rolled/manual signature scheme. Private
signing keys are never read, generated, stored, or logged here: every
function takes its key as an explicit argument supplied by the caller,
and nothing in this module persists key material anywhere.

load_package_signing_private_key() (Task 15 / Task 13 Corrective A) is
the one exception: it is Central's fixed-path operator-configuration
loader for that same private key (Amendment C's Central-side
counterpart), documented in docs/operator/configuration.md as supplied
out of band at /etc/linuxlab/keys/package-signing.key. It reads the key
only at invocation time, never at module import, and never logs or
returns anything but the parsed key object itself.
"""

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

SIGNATURE_ALGORITHM = "ed25519"

_SIGNING_KEY_PATH = Path("/etc/linuxlab/keys/package-signing.key")


class PackageSigningConfigurationError(Exception):
    pass


def sign_bytes(message, private_key):
    """Sign `message` with an Ed25519 private key, returning the raw
    64-byte signature. Deterministic for a given (message, key) pair --
    an inherent Ed25519 property, not something this wrapper adds.
    """
    return private_key.sign(message)


def verify_signature(message, signature, public_key):
    """Verify `signature` over `message` against an Ed25519 public key.

    Raises cryptography.exceptions.InvalidSignature on failure. Left
    untranslated here deliberately: this module is a thin, library-
    transparent primitive, not the public domain boundary -- callers that
    need a package-specific exception (e.g. verify_package()) translate at
    their own boundary instead.
    """
    public_key.verify(signature, message)


def load_package_signing_private_key():
    try:
        pem_bytes = _SIGNING_KEY_PATH.read_bytes()
    except OSError as exc:
        raise PackageSigningConfigurationError(
            f"cannot read Central signing key at {_SIGNING_KEY_PATH}: {exc.strerror}"
        ) from exc

    if not pem_bytes:
        raise PackageSigningConfigurationError(
            f"Central signing key at {_SIGNING_KEY_PATH} is empty"
        )

    try:
        private_key = load_pem_private_key(pem_bytes, password=None)
    except (ValueError, TypeError) as exc:
        raise PackageSigningConfigurationError(
            f"Central signing key at {_SIGNING_KEY_PATH} is not valid PEM: {exc}"
        ) from exc

    if not isinstance(private_key, Ed25519PrivateKey):
        raise PackageSigningConfigurationError(
            f"Central signing key at {_SIGNING_KEY_PATH} is not an Ed25519 "
            f"private key (got {type(private_key).__name__})"
        )

    return private_key
