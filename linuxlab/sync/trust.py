"""Node's fixed local Ed25519 trust anchor for verify_package()/
import_package() (Amendment C, docs/superpowers/specs/
2026-09-11-task15-task13-package-operator-surface-design.md Section 5a).
"""

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

_TRUST_ANCHOR_PATH = Path("/etc/linuxlab/keys/package-signing-public.key")


class PackageTrustConfigurationError(Exception):
    pass


def load_package_trusted_public_key():
    try:
        pem_bytes = _TRUST_ANCHOR_PATH.read_bytes()
    except OSError as exc:
        raise PackageTrustConfigurationError(
            f"cannot read Node trust anchor at {_TRUST_ANCHOR_PATH}: {exc.strerror}"
        ) from exc

    if not pem_bytes:
        raise PackageTrustConfigurationError(
            f"Node trust anchor at {_TRUST_ANCHOR_PATH} is empty"
        )

    try:
        public_key = load_pem_public_key(pem_bytes)
    except ValueError as exc:
        raise PackageTrustConfigurationError(
            f"Node trust anchor at {_TRUST_ANCHOR_PATH} is not valid PEM: {exc}"
        ) from exc

    if not isinstance(public_key, Ed25519PublicKey):
        raise PackageTrustConfigurationError(
            f"Node trust anchor at {_TRUST_ANCHOR_PATH} is not an Ed25519 public key "
            f"(got {type(public_key).__name__})"
        )

    return public_key
