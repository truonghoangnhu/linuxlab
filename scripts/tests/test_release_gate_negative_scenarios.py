"""Task 15 Task 9: exactly five release-blocking negative scenarios
(T15-D09), against the same real dual-deployment infrastructure Task 8
uses. Each scenario lives in scripts.release_gate.scenarios (Task 11
extraction) so this test file and scripts/phase1_release_gate.py both
exercise the exact same real-infra logic -- real ephemeral PostgreSQL
databases, real Gunicorn processes, real HTTP, the locked
assessment/single_choice provider. Each scenario asserts both the
desired final state and the forbidden invariant that must not occur.
No chaos framework, no fake trusted state, no raw ORM/SQL fabrication
of trusted state.
"""

from scripts.release_gate import scenarios
from scripts.release_gate.e2e_support import dual_deployment


def test_n1_tampered_replacement_leaves_existing_active_package_fully_unchanged(tmp_path):
    """Desired final state: Node already has a real, valid, ACTIVE
    package A (same identity/hash, still HTTP-usable, installed
    artifacts intact) BEFORE the attack, and A remains in exactly that
    state afterward. Forbidden invariant: A is archived/deactivated/
    replaced by the rejected import, or any partial trusted row for the
    tampered replacement B is created. No mocked verifier failure --
    B is a real republish of the SAME source, really signed, then really
    tampered, and rejected through the real Task 11/12 public import
    lifecycle API."""
    with dual_deployment(tmp_path) as deployment:
        scenarios.run_n1_tampered_replacement(deployment)


def test_n2_revoked_credential_never_falsely_delivers_and_leaks_no_secret(tmp_path):
    """Desired final state: Central rejects; Outbox stays pending/
    retryable. Forbidden invariant: the Outbox row becomes DELIVERED, or
    the Node's bearer secret appears in the worker's own stdout/stderr."""
    with dual_deployment(tmp_path) as deployment:
        detail = scenarios.run_n2_revoked_credential(deployment)

    # Task 15 Task 11 corrective v3, Section 13: n2's mandatory typed
    # assertion set.
    assert detail["request_rejected"] is True
    assert detail["central_inbox_mutated"] is False
    assert detail["revoked_credential_not_accepted"] is True


def test_n3_ack_lost_then_retry_yields_exactly_one_inbox_and_eventual_delivery(tmp_path):
    """Desired final state: Central durably stores exactly one Inbox row
    across both the original delivery and the retry; Node eventually
    reaches DELIVERED. Forbidden invariant: a second Inbox row for the
    same event, or the retry being treated as a fresh/duplicate business
    event rather than the same idempotent event_id.

    ACK loss is a REAL commit-before-ACK-loss boundary: the Node's real
    HttpSyncTransport talks to a real one-shot response-dropping TCP
    proxy (scripts.release_gate.one_shot_proxy) in front of the real
    Central HTTP endpoint."""
    with dual_deployment(tmp_path) as deployment:
        scenarios.run_n3_ack_lost_real_proxy(deployment)


def test_n4_central_unavailable_node_keeps_learning_and_syncs_after_recovery(tmp_path):
    """Desired final state: while Central is down, the Node HTTP learner
    flow (grading included) still works end to end, and the resulting
    Outbox row stays durable; once Central returns, a normal sync cycle
    delivers it. Forbidden invariant: any learner-facing failure while
    Central is down, or a lost/dropped event once Central returns."""
    with dual_deployment(tmp_path) as deployment:
        detail = scenarios.run_n4_central_unavailable(deployment)

    # Task 15 Task 11 corrective v3, Section 13: n4's mandatory typed
    # assertion set.
    assert detail["local_runtime_available"] is True
    assert detail["outbox_remained_durable"] is True
    assert detail["no_event_loss"] is True
    assert detail["recovery_delivery_success"] is True


def test_n5_node_restart_preserves_active_package_and_pending_outbox(tmp_path):
    """Desired final state: after a Node process restart (same database,
    fresh Gunicorn process), the active package is still usable, the
    pending Outbox row survived, readiness recovers, and sync can resume.
    Forbidden invariant: the active package or the pending Outbox row is
    lost/reset by the restart."""
    with dual_deployment(tmp_path) as deployment:
        detail = scenarios.run_n5_node_restart(deployment)

    # Task 15 Task 11 corrective v3, Section 13: n5's mandatory typed
    # assertion set.
    assert detail["active_package_survived_restart"] is True
    assert detail["pending_outbox_survived_restart"] is True
    assert detail["runtime_recovered"] is True
    assert detail["delivery_resumed"] is True
