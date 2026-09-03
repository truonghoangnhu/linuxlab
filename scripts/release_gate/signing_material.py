"""Task 15 Tasks 7-10 corrective, Critical 1: ephemeral Ed25519 signing
keypair lifecycle (private-key-lifecycle hard invariant).

The private key is created with exclusive atomic 0600 permissions --
`os.open(..., O_CREAT|O_EXCL|O_WRONLY, 0o600)` -- never written with a
default mode and chmod'd afterward, and never dependent on umask. There
is no window in which the file exists on disk at a broader mode than
0600.

Deletion is a single idempotent function (`delete_private_key()`) so
every caller-side exit path -- success, assertion failure inside a
`with dual_deployment(...)` block, or a partial-setup failure before
that block ever yields -- can invoke exactly the same cleanup, from a
plain `finally` around a locally tracked path variable, without caring
whether the key was ever actually created.
"""

import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

PRIVATE_KEY_MODE = 0o600
PRIVATE_KEY_FILENAME = "release-gate-signing.pem"
PUBLIC_KEY_FILENAME = "release-gate-trusted.pem"


def _create_private_key_file_atomically(path: Path, pem_bytes: bytes) -> None:
    """Task 15 Tasks 7-10 final cleanup corrective: the exclusive open()
    mode alone is not sufficient to guarantee the exact final mode --
    the kernel ANDs it against the process umask, so a sufficiently
    restrictive umask (e.g. 0o777) can silently narrow the actual
    created mode below 0o600. fchmod(fd, 0o600) establishes the exact
    mode explicitly, independent of umask, and fstat(fd) verifies it
    actually took effect -- both BEFORE a single byte of key material is
    written. No step here ever mutates the process-global umask."""
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_KEY_MODE)
    try:
        os.fchmod(fd, PRIVATE_KEY_MODE)
        actual_mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if actual_mode != PRIVATE_KEY_MODE:
            raise OSError(
                f"private key file {path} has mode {oct(actual_mode)} after fchmod, "
                f"expected {oct(PRIVATE_KEY_MODE)} -- refusing to write key material"
            )
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem_bytes)
    except BaseException:
        delete_private_key(path)
        raise


def create_ephemeral_signing_keypair(tmp_dir):
    """Generates a fresh Ed25519 keypair and writes BOTH the private key
    (exclusive atomic creation, fchmod+fstat-verified 0600 before any
    key bytes are written) and the public key to disk. Returns
    (private_key_path, public_key_path) only once BOTH have succeeded.

    Transactional: this function owns both paths until it returns. If
    the public key write fails after the private key was already
    created, the already-created private key is deleted, any public
    partial is deleted, and the exception propagates -- the caller never
    receives ownership of a half-written keypair to clean up itself.
    Deletion of a successfully returned private key remains the
    caller's own responsibility via delete_private_key()."""
    tmp_dir = Path(tmp_dir)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_key_path = tmp_dir / PRIVATE_KEY_FILENAME
    public_key_path = tmp_dir / PUBLIC_KEY_FILENAME

    _create_private_key_file_atomically(
        private_key_path,
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()),
    )
    try:
        public_key_path.write_bytes(
            public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        )
    except BaseException:
        delete_private_key(private_key_path)
        Path(public_key_path).unlink(missing_ok=True)
        raise
    return private_key_path, public_key_path


def delete_private_key(path) -> None:
    """Idempotent: a safe no-op if `path` was never created or has
    already been deleted, since cleanup runs unconditionally on every
    exit path regardless of how far setup got."""
    Path(path).unlink(missing_ok=True)
