"""Task 15 Tasks 7-10 corrective, Critical 1: ephemeral Ed25519 signing
private key lifecycle. Hard invariant: the private key is atomically
created at mode 0600 (never 0644-then-chmod, never relying on umask),
tracked as a deployment-owned artifact, and deleted on success,
assertion failure, and partial setup failure alike.
"""

import os
import stat

import pytest

from scripts.release_gate import signing_material
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


# --- Task 15 Tasks 7-10 final cleanup corrective: mode-before-write --------


def test_private_key_mode_is_exactly_0600_under_a_highly_restrictive_umask(tmp_path):
    """A umask of 0o777 blocks every permission bit -- the requested
    open() mode ANDed with ~umask alone would yield 0000, not 0600.
    fchmod(fd, 0o600) must be what actually establishes the exact final
    mode, never process umask. The umask is process-global state, so it
    is restored in `finally` no matter what, before any assertion that
    could fail and leave every later test in this process corrupted."""
    old_umask = os.umask(0o777)
    try:
        private_key_path, public_key_path = create_ephemeral_signing_keypair(tmp_path)
    finally:
        os.umask(old_umask)

    try:
        assert _mode(private_key_path) == 0o600
    finally:
        delete_private_key(private_key_path)
        public_key_path.unlink(missing_ok=True)


def test_private_key_bytes_are_written_only_after_fchmod_and_fstat_verification(tmp_path):
    """Real ordering proof (spies wrapping the real os.fchmod/os.fstat/
    os.fdopen calls, not a hand-simulated result): the private key's own
    open() happens first (mode requested at open time), but fchmod() and
    the fstat() verification of the resulting mode must both happen
    BEFORE any byte of key material is written to the file."""
    calls = []

    real_fchmod = os.fchmod

    def _spy_fchmod(fd, mode):
        calls.append("fchmod")
        return real_fchmod(fd, mode)

    real_fstat = os.fstat

    def _spy_fstat(fd):
        calls.append("fstat")
        return real_fstat(fd)

    real_fdopen = os.fdopen

    def _spy_fdopen(fd, mode, *args, **kwargs):
        handle = real_fdopen(fd, mode, *args, **kwargs)
        real_write = handle.write

        def _spy_write(data):
            calls.append("write")
            return real_write(data)

        handle.write = _spy_write
        return handle

    original_fchmod, original_fstat, original_fdopen = os.fchmod, os.fstat, os.fdopen
    os.fchmod, os.fstat, os.fdopen = _spy_fchmod, _spy_fstat, _spy_fdopen
    try:
        private_key_path, public_key_path = create_ephemeral_signing_keypair(tmp_path)
    finally:
        os.fchmod, os.fstat, os.fdopen = original_fchmod, original_fstat, original_fdopen

    try:
        assert "fchmod" in calls
        assert "fstat" in calls
        assert "write" in calls
        assert calls.index("fchmod") < calls.index("write")
        assert calls.index("fstat") < calls.index("write")
    finally:
        delete_private_key(private_key_path)
        public_key_path.unlink(missing_ok=True)


# --- Task 15 Tasks 7-10 final cleanup corrective: transactional keypair ----


def test_public_key_write_failure_leaves_no_private_or_public_material(tmp_path, monkeypatch):
    """create_ephemeral_signing_keypair() must own BOTH paths until both
    succeed: if the public key write fails after the private key was
    already created, the already-created private key must be deleted,
    any public partial must be deleted, and no ownership is returned to
    the caller (the exception propagates instead)."""
    from pathlib import Path

    real_write_bytes = Path.write_bytes

    def _fail_on_public_key(self, data):
        if self.name == signing_material.PUBLIC_KEY_FILENAME:
            raise OSError("simulated public key write failure")
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", _fail_on_public_key)

    with pytest.raises(OSError, match="simulated public key write failure"):
        create_ephemeral_signing_keypair(tmp_path)

    assert not (tmp_path / signing_material.PRIVATE_KEY_FILENAME).exists()
    assert not (tmp_path / signing_material.PUBLIC_KEY_FILENAME).exists()
