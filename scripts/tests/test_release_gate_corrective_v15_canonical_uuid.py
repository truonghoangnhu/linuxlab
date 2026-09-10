"""Task 15 Task 11 corrective v15 (bounded closure), Section 4: closes
the independent v14 review's `OutboxInboxProof` constructor canonical-
UUID gap.

`OutboxInboxProof.__post_init__()` already required an exact `str` and
that `uuid.UUID(value)` parse successfully, but `UUID(value)` happily
PARSES a noncanonical-but-well-formed representation (uppercase,
unhyphenated) without complaint -- the constructor retained that
original noncanonical string by identity instead of rejecting it. Only
`validate_for_pass()` (via `_is_canonical_uuid_string()`) closed this
at the FINAL boundary; the constructor itself did not.

These are the mandatory RED cases from the v15 corrective prompt,
Section 4A.
"""

import uuid
from pathlib import Path

import pytest

from scripts.release_gate.evidence import InvalidProofError, OutboxInboxProof

# Deliberately contains hex letters (a-f) -- an all-digit UUID would make
# `.upper()` a silent no-op and defeat the uppercase-rejection test below.
_VALID_CANONICAL_UUID = "abcdefab-1234-5678-9abc-def012345678"


def _proof_with(event_uuid) -> OutboxInboxProof:
    return OutboxInboxProof(
        event_uuid=event_uuid,
        inbox_count=1,
        node_state="DELIVERED",
        duplicate_idempotent=True,
    )


class _EvilStr(str):
    pass


def test_valid_lowercase_hyphenated_uuid_is_accepted_unchanged():
    proof = _proof_with(_VALID_CANONICAL_UUID)
    assert proof.event_uuid == _VALID_CANONICAL_UUID


def test_uppercase_uuid_is_rejected():
    with pytest.raises(InvalidProofError):
        _proof_with(_VALID_CANONICAL_UUID.upper())


def test_unhyphenated_uuid_is_rejected():
    with pytest.raises(InvalidProofError):
        _proof_with(_VALID_CANONICAL_UUID.replace("-", ""))


def test_raw_uuid_object_is_rejected():
    with pytest.raises(InvalidProofError):
        _proof_with(uuid.UUID(_VALID_CANONICAL_UUID))


def test_string_subclass_is_rejected():
    with pytest.raises(InvalidProofError):
        _proof_with(_EvilStr(_VALID_CANONICAL_UUID))


def test_int_is_rejected():
    with pytest.raises(InvalidProofError):
        _proof_with(111111111111111111111111111111)


def test_bytes_is_rejected():
    with pytest.raises(InvalidProofError):
        _proof_with(_VALID_CANONICAL_UUID.encode("ascii"))


def test_path_is_rejected():
    with pytest.raises(InvalidProofError):
        _proof_with(Path(_VALID_CANONICAL_UUID))


def test_malformed_exact_string_is_rejected():
    with pytest.raises(InvalidProofError):
        _proof_with("not-a-uuid-at-all")


def test_braced_uuid_representation_is_rejected():
    with pytest.raises(InvalidProofError):
        _proof_with("{" + _VALID_CANONICAL_UUID + "}")
