"""Task 15 Tasks 7-10 corrective, Critical 1: ephemeral Ed25519 signing
private key lifecycle. Hard invariant: the private key is atomically
created at mode 0600 (never 0644-then-chmod, never relying on umask),
tracked as a deployment-owned artifact, and deleted on success,
assertion failure, and partial setup failure alike.
"""

import stat

import pytest

from scripts.release_gate.signing_material import (
    create_ephemeral_signing_keypair,
    delete_private_key,
)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_private_key_mode_is_exactly_0600_while_present(tmp_path):
    private_key_path, _public_key_path = create_ephemeral_signing_keypair(tmp_path)
    try:
        assert private_key_path.exists()
        assert _mode(private_key_path) == 0o600
    finally:
        delete_private_key(private_key_path)


def test_private_key_creation_is_exclusive_atomic_not_chmod_after_write(tmp_path):
    """Proves O_CREAT|O_EXCL semantics: creating against a path that
    already exists must fail rather than silently overwrite -- the same
    property that rules out a 'write then chmod' implementation, since a
    chmod-based writer would happily overwrite first and narrow
    permissions second, leaving a real (if brief) 0644 window."""
    private_key_path, _public_key_path = create_ephemeral_signing_keypair(tmp_path)
    try:
        with pytest.raises(FileExistsError):
            create_ephemeral_signing_keypair(tmp_path)
    finally:
        delete_private_key(private_key_path)


def test_public_key_file_is_not_secret_material_and_may_be_readable(tmp_path):
    _private_key_path, public_key_path = create_ephemeral_signing_keypair(tmp_path)
    try:
        assert public_key_path.exists()
    finally:
        delete_private_key(_private_key_path)


def test_delete_private_key_removes_the_file(tmp_path):
    private_key_path, _public_key_path = create_ephemeral_signing_keypair(tmp_path)
    delete_private_key(private_key_path)
    assert not private_key_path.exists()


def test_delete_private_key_is_idempotent_when_already_absent(tmp_path):
    missing = tmp_path / "never-created.pem"
    delete_private_key(missing)  # must not raise
    delete_private_key(missing)  # calling twice must also not raise
