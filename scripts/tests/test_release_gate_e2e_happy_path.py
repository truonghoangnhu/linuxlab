"""Task 15 Task 8: real dual-deployment happy-path E2E (T15-D02, T15-D09,
T15-D17, T15-D18, T15-D20, T15-D27).

The scenario itself lives in scripts.release_gate.scenarios.run_happy_path()
(Task 11 extraction) so this test and scripts/phase1_release_gate.py both
exercise the exact same real-infra logic -- two genuinely separate
ephemeral PostgreSQL databases, two real Gunicorn processes, real HTTP
throughout, an ephemeral Ed25519 signing key, a real signed `.llpkg`,
the locked production assessment/single_choice provider, a real issued
Node sync credential, and the real HttpSyncTransport delivering to
Central's real /api/v1/sync/events/ -- no Django test-client
substitute for the release scenario, no browser automation.
"""

from scripts.release_gate import scenarios
from scripts.release_gate.e2e_support import dual_deployment


def test_real_dual_deployment_happy_path(tmp_path):
    with dual_deployment(tmp_path) as deployment:
        detail = scenarios.run_happy_path(deployment)

    # Required release evidence content (Task 15 Task 11, design spec
    # Section 17.1): package SHA-256 and the ephemeral signing public
    # key's fingerprint must both be present in what the scenario
    # reports -- neither is secret material (the fingerprint is a hash
    # of the PUBLIC key; the package SHA-256 identifies published,
    # non-secret content).
    assert len(detail["package_sha256"]) == 64
    assert all(c in "0123456789abcdef" for c in detail["package_sha256"])
    assert len(detail["public_key_fingerprint"]) == 64
    assert all(c in "0123456789abcdef" for c in detail["public_key_fingerprint"])

    # Task 15 Task 11 corrective, Evidence v1 Section 7 "proofs":
    # signature verification result and the Outbox->Inbox delivery
    # proof must also be present -- both real facts of THIS run, not
    # placeholders (a successful import_and_activate() IS the real
    # signature-verification proof; the delivered/inbox-found sequence
    # already asserted above IS the real Outbox->Inbox proof).
    #
    # Task 15 Task 11 corrective v2, Section 7.3: outbox_to_inbox is a
    # TYPED structure (event_uuid, inbox_count, node_state,
    # duplicate_idempotent), never a bare "delivered" string -- built
    # from the exact same already-asserted facts above (event_id,
    # count-inbox-messages == 1, the resend-is-duplicate check).
    assert detail["signature_verified"] is True
    outbox_to_inbox = detail["outbox_to_inbox"]
    assert outbox_to_inbox["event_uuid"] == detail["event_id"]
    assert outbox_to_inbox["inbox_count"] == 1
    assert outbox_to_inbox["node_state"] == "DELIVERED"
    assert outbox_to_inbox["duplicate_idempotent"] is True
