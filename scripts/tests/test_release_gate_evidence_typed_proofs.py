"""Task 15 Task 11 corrective v2, Section 7 ("Typed proof model") and
Section 18.1 ("Typed proof rejection"): field-name allowlisting alone is
insufficient because arbitrary VALUES can still cross the evidence
boundary under an allowlisted key. Every proof value must be typed and
validated before it is ever admitted into EvidenceV1 -- a malformed
value (wrong length, wrong case, a credential-like or answer-like
string, an Authorization-header-like string, or any other untyped
value) must be rejected, never silently serialized.
"""

import pytest

from scripts.release_gate.evidence import (
    InvalidProofError,
    OutboxInboxProof,
    SignatureProof,
    build_typed_proofs,
    validate_sha256_hex,
)

VALID_SHA256 = "a" * 64


# --- Sha256Hex -------------------------------------------------------------


def test_validate_sha256_hex_accepts_a_well_formed_digest():
    assert validate_sha256_hex(VALID_SHA256, field_name="package_sha256") == VALID_SHA256


@pytest.mark.parametrize(
    "bad_value",
    [
        "a" * 63,  # too short
        "a" * 65,  # too long
        "A" * 64,  # uppercase
        "g" * 64,  # non-hex character
        "Authorization: Bearer sentinel-token-abc123",  # header-like
        "correct_option_id=a",  # answer-like
        "sentinel-node-secret-9f3d2c1a",  # credential-like
        "",
        None,
        12345,
        {"nested": "dict"},
    ],
)
def test_validate_sha256_hex_rejects_malformed_values(bad_value):
    with pytest.raises(InvalidProofError):
        validate_sha256_hex(bad_value, field_name="package_sha256")


# --- SignatureProof ----------------------------------------------------


def test_signature_proof_accepts_a_bool():
    proof = SignatureProof(verified=True)
    assert proof.to_dict() == {"verified": True}


@pytest.mark.parametrize("bad_value", ["true", 1, None, "verified"])
def test_signature_proof_rejects_non_bool(bad_value):
    with pytest.raises(InvalidProofError):
        SignatureProof(verified=bad_value)


# --- OutboxInboxProof ----------------------------------------------------


def _valid_outbox_kwargs(**overrides):
    kwargs = {
        "event_uuid": "12345678-1234-5678-1234-567812345678",
        "inbox_count": 1,
        "node_state": "DELIVERED",
        "duplicate_idempotent": True,
    }
    kwargs.update(overrides)
    return kwargs


def test_outbox_inbox_proof_accepts_well_formed_fields():
    proof = OutboxInboxProof(**_valid_outbox_kwargs())
    assert proof.to_dict() == {
        "event_uuid": "12345678-1234-5678-1234-567812345678",
        "inbox_count": 1,
        "node_state": "DELIVERED",
        "duplicate_idempotent": True,
    }


def test_outbox_inbox_proof_rejects_malformed_event_uuid():
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**_valid_outbox_kwargs(event_uuid="not-a-uuid"))


@pytest.mark.parametrize("bad_count", [-1, "1", 1.5, None, True])
def test_outbox_inbox_proof_rejects_malformed_inbox_count(bad_count):
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**_valid_outbox_kwargs(inbox_count=bad_count))


@pytest.mark.parametrize("bad_state", ["delivered", "SENT", "", None, 1])
def test_outbox_inbox_proof_rejects_unknown_node_state(bad_state):
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**_valid_outbox_kwargs(node_state=bad_state))


@pytest.mark.parametrize("bad_flag", ["true", 1, None])
def test_outbox_inbox_proof_rejects_non_bool_duplicate_idempotent(bad_flag):
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**_valid_outbox_kwargs(duplicate_idempotent=bad_flag))


# --- build_typed_proofs: the evidence boundary ---------------------------


def test_build_typed_proofs_accepts_a_full_well_formed_set():
    raw = {
        "package_sha256": VALID_SHA256,
        "payload_sha256": "b" * 64,
        "public_key_fingerprint": "c" * 64,
        "signature_verified": True,
        "outbox_to_inbox": {
            "event_uuid": "12345678-1234-5678-1234-567812345678",
            "inbox_count": 1,
            "node_state": "DELIVERED",
            "duplicate_idempotent": True,
        },
    }
    proofs = build_typed_proofs(raw)
    assert proofs["package_sha256"] == VALID_SHA256
    assert proofs["payload_sha256"] == "b" * 64
    assert proofs["public_key_fingerprint"] == "c" * 64
    assert proofs["signature"] == {"verified": True}
    assert proofs["outbox_to_inbox"] == {
        "event_uuid": "12345678-1234-5678-1234-567812345678",
        "inbox_count": 1,
        "node_state": "DELIVERED",
        "duplicate_idempotent": True,
    }


def test_build_typed_proofs_ignores_unknown_keys():
    """An arbitrary scenario-returned key not in the typed allowlist
    must never reach evidence -- it is silently dropped, not an error,
    since a scenario detail dict may legitimately carry other bookkeeping
    fields (e.g. event_id) that are not release-evidence proofs."""
    proofs = build_typed_proofs({"package_sha256": VALID_SHA256, "not_a_proof_field": "arbitrary"})
    assert proofs == {"package_sha256": VALID_SHA256}
    assert "not_a_proof_field" not in proofs


@pytest.mark.parametrize(
    "field_name,bad_value",
    [
        ("package_sha256", "A" * 64),
        ("package_sha256", "sentinel-answer-key-material"),
        ("payload_sha256", "Authorization: Bearer abc"),
        ("public_key_fingerprint", "short"),
        ("signature_verified", "yes"),
        ("outbox_to_inbox", "delivered"),  # legacy bare-string form, no longer accepted
        ("outbox_to_inbox", {"event_uuid": "not-a-uuid", "inbox_count": 1, "node_state": "DELIVERED", "duplicate_idempotent": True}),
    ],
)
def test_build_typed_proofs_rejects_malformed_values(field_name, bad_value):
    with pytest.raises(InvalidProofError):
        build_typed_proofs({field_name: bad_value})


def test_build_typed_proofs_merges_across_multiple_scenario_detail_dicts_first_wins():
    proofs = build_typed_proofs(
        {"package_sha256": VALID_SHA256},
        {"package_sha256": "b" * 64, "payload_sha256": "c" * 64},
    )
    assert proofs["package_sha256"] == VALID_SHA256  # first-seen wins
    assert proofs["payload_sha256"] == "c" * 64
