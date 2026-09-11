"""Node's fixed local Ed25519 trust anchor for verify_package()/
import_package() (Amendment C, docs/superpowers/specs/
2026-09-11-task15-task13-package-operator-surface-design.md Section 5a).
"""

import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

_TRUST_ANCHOR_PATH = Path("/etc/linuxlab/keys/package-signing-public.key")


class PackageTrustConfigurationError(Exception):
    pass


def _read_trust_anchor_bytes():
    """Open the canonical trust-anchor path with O_NOFOLLOW (a symlink at
    that path is rejected outright, never followed) and O_NONBLOCK (so a
    FIFO can never block this call), then fstat() and read from that SAME
    descriptor -- never a separate pre-open stat()/is_file() check, which
    would leave a TOCTOU window between checking and opening. Only an
    ordinary regular file is accepted.
    """
    try:
        fd = os.open(str(_TRUST_ANCHOR_PATH), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise PackageTrustConfigurationError(
            f"cannot read Node trust anchor at {_TRUST_ANCHOR_PATH}: {exc.strerror}"
        ) from exc

    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PackageTrustConfigurationError(
                f"Node trust anchor at {_TRUST_ANCHOR_PATH} is not a regular file"
            )
        handle = os.fdopen(fd, "rb")
        fd = None  # ownership transferred to `handle`; its own close below
        # is now the only close
        with handle:
            return handle.read()
    finally:
        if fd is not None:
            os.close(fd)


def load_package_trusted_public_key():
    pem_bytes = _read_trust_anchor_bytes()

    if not pem_bytes:
        raise PackageTrustConfigurationError(
            f"Node trust anchor at {_TRUST_ANCHOR_PATH} is empty"
        )

    try:
        public_key = load_pem_public_key(pem_bytes)
    except ValueError as exc:
        raise PackageTrustConfigurationError(
            f"Node trust anchor at {_TRUST_ANCHOR_PATH} is not valid PEM"
        ) from exc

    if not isinstance(public_key, Ed25519PublicKey):
        raise PackageTrustConfigurationError(
            f"Node trust anchor at {_TRUST_ANCHOR_PATH} is not an Ed25519 public key "
            f"(got {type(public_key).__name__})"
        )

    return public_key
