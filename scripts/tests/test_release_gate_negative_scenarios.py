"""Task 15 Task 9: exactly five release-blocking negative scenarios
(T15-D09), against the same real dual-deployment infrastructure Task 8
uses -- real ephemeral PostgreSQL databases, real Gunicorn processes,
real HTTP, the locked assessment/single_choice provider. Each scenario
asserts both the desired final state and the forbidden invariant that
must not occur. No chaos framework, no fake trusted state, no raw
ORM/SQL fabrication of trusted state.
"""

import io
import re
import time
import zipfile

import pytest

from linuxlab.publication.package import SIGNATURE_MEMBER
from scripts.release_gate import http_bridge
from scripts.release_gate.e2e_support import (
    ActionError,
    author_and_package,
    dual_deployment,
    import_and_activate,
    run_action,
    run_sync_worker_once,
)
from scripts.release_gate.processes import (
    reserve_listener,
    start_gunicorn,
    stop_process,
    wait_ready,
)

_CONTENT = {
    "prompt": "Which command lists files?",
    "options": [{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
}


def _question_public_id_from_html(html):
    match = re.search(r'data-question-public-id="([0-9a-f-]{36})"', html)
    assert match
    return match.group(1)


def _flush_until_delivered(node_profile_env, *, deadline_seconds=30, poll_interval_seconds=1.0):
    """Repeatedly triggers the real sync worker until it reports at least
    one delivery, bounded by a real deadline -- never a bypass of the
    real exponential backoff a transient-failure retry is subject to
    (release_transient()'s next_attempt_at), just waiting it out exactly
    as an operator/monitoring loop would."""
    deadline = time.monotonic() + deadline_seconds
    last_result = None
    while time.monotonic() < deadline:
        last_result = run_sync_worker_once(node_profile_env)
        if last_result.returncode == 0 and "delivered=1" in last_result.stdout:
            return last_result
        time.sleep(poll_interval_seconds)
    return last_result


def _tamper_signature(package_path):
    package_bytes = package_path.read_bytes()
    reader = zipfile.ZipFile(io.BytesIO(package_bytes))
    members = {name: reader.read(name) for name in reader.namelist()}
    signature = bytearray(members[SIGNATURE_MEMBER])
    signature[0] ^= 0xFF
    members[SIGNATURE_MEMBER] = bytes(signature)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as writer:
        for name, data in members.items():
            writer.writestr(name, data)
    tampered_path = package_path.with_name(package_path.name + ".tampered")
    tampered_path.write_bytes(buffer.getvalue())
    return tampered_path


def _submit_a_real_attempt(deployment, authored):
    """Shared setup for N2/N3/N4/N5: run the real learner HTTP flow once
    against an already-activated package, producing a real pending
    Outbox row to exercise sync behavior against."""
    session = http_bridge.LearnerSession.start(deployment.node.base_url)
    http_bridge.get_landing(session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"])
    _status, attempt_url_path = http_bridge.post_start(
        session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
    )
    _status, attempt_body = http_bridge.get_attempt(session, attempt_url_path)
    question_public_id = _question_public_id_from_html(attempt_body)
    status, submit_body = http_bridge.post_submit(
        session,
        attempt_url_path,
        [
            {
                "question_public_id": question_public_id,
                "question_version": authored["question_version"],
                "selected_option_id": "a",
            }
        ],
    )
    assert status == 200
    return session, attempt_url_path, submit_body


# --- N1: invalid signature ---------------------------------------------------


def test_n1_invalid_signature_leaves_no_partial_trusted_inventory(tmp_path):
    """Desired final state: import is rejected. Forbidden invariant: any
    InstalledPackage row (even a rejected/half-written one) exists for
    this source afterward."""
    with dual_deployment(tmp_path) as deployment:
        authored, package_path = author_and_package(
            deployment, prompt=_CONTENT["prompt"], options=_CONTENT["options"], correct_option_id="a"
        )
        tampered_path = _tamper_signature(package_path)

        with pytest.raises(ActionError, match="PackageImportError"):
            import_and_activate(deployment, tampered_path)

        count = run_action(
            deployment.node_profile_env,
            "count-installed-packages",
            "--package-type",
            "learning_path",
            "--source-public-id",
            authored["source_public_id"],
        )
        assert count["count"] == 0

        active = run_action(
            deployment.node_profile_env,
            "find-active-package",
            "--package-type",
            "learning_path",
            "--source-public-id",
            authored["source_public_id"],
        )
        assert active["found"] is False


# --- N2: invalid/revoked credential ------------------------------------------


def test_n2_revoked_credential_never_falsely_delivers_and_leaks_no_secret(tmp_path):
    """Desired final state: Central rejects; Outbox stays pending/
    retryable. Forbidden invariant: the Outbox row becomes DELIVERED, or
    the Node's bearer secret appears in the worker's own stdout/stderr."""
    with dual_deployment(tmp_path) as deployment:
        authored, package_path = author_and_package(
            deployment, prompt=_CONTENT["prompt"], options=_CONTENT["options"], correct_option_id="a"
        )
        import_and_activate(deployment, package_path)
        _session, _attempt_url, submit_body = _submit_a_real_attempt(deployment, authored)
        attempt_public_id = submit_body["attempt_public_id"]

        outbox_before = run_action(
            deployment.node_profile_env, "find-outbox-message-by-attempt", "--attempt-public-id", attempt_public_id
        )
        assert outbox_before["state"] == "pending"

        run_action(deployment.central_profile_env, "revoke-node-credential", "--node-id", deployment.node_id)

        flush_result = run_sync_worker_once(deployment.node_profile_env)
        assert flush_result.returncode == 0
        assert "auth_fault=True" in flush_result.stdout
        assert "delivered=0" in flush_result.stdout
        assert deployment.node_secret not in flush_result.stdout
        assert deployment.node_secret not in flush_result.stderr

        outbox_after = run_action(
            deployment.node_profile_env,
            "find-outbox-message",
            "--event-id",
            outbox_before["event_id"],
        )
        assert outbox_after["state"] == "pending"


# --- N3: ACK lost after durable Central Inbox --------------------------------


def test_n3_ack_lost_then_retry_yields_exactly_one_inbox_and_eventual_delivery(tmp_path):
    """Desired final state: Central durably stores exactly one Inbox row
    across both the original delivery and the retry; Node eventually
    reaches DELIVERED. Forbidden invariant: a second Inbox row for the
    same event, or the retry being treated as a fresh/duplicate business
    event rather than the same idempotent event_id."""
    with dual_deployment(tmp_path) as deployment:
        authored, package_path = author_and_package(
            deployment, prompt=_CONTENT["prompt"], options=_CONTENT["options"], correct_option_id="a"
        )
        import_and_activate(deployment, package_path)
        _session, _attempt_url, submit_body = _submit_a_real_attempt(deployment, authored)
        attempt_public_id = submit_body["attempt_public_id"]

        outbox = run_action(
            deployment.node_profile_env, "find-outbox-message-by-attempt", "--attempt-public-id", attempt_public_id
        )
        event_id = outbox["event_id"]

        # "ACK lost": deliver the envelope directly (bypassing
        # flush_outbox()'s own delivery-state bookkeeping) so Central
        # durably stores it while the Node's local Outbox row never
        # learns of the ACK -- exactly what a genuinely lost response
        # looks like from the Node's own state, achieved honestly rather
        # than through network-level fault injection.
        first_send = run_action(
            deployment.node_profile_env,
            "resend-outbox-message",
            "--event-id",
            event_id,
            "--central-url",
            deployment.central.base_url,
            "--node-id",
            deployment.node_id,
            stdin_json={"secret": deployment.node_secret},
        )
        assert first_send["results"] == [{"event_id": event_id, "status": "accepted", "code": ""}]
        assert run_action(deployment.central_profile_env, "count-inbox-messages", "--event-id", event_id)["count"] == 1

        outbox_still_pending = run_action(
            deployment.node_profile_env, "find-outbox-message", "--event-id", event_id
        )
        assert outbox_still_pending["state"] == "pending"

        # Node retries via its real worker path: Central recognizes the
        # same event_id and returns DUPLICATE, which flush_outbox()
        # itself already treats as a delivered confirmation.
        flush_result = run_sync_worker_once(deployment.node_profile_env)
        assert flush_result.returncode == 0
        assert "delivered=1" in flush_result.stdout

        assert run_action(deployment.central_profile_env, "count-inbox-messages", "--event-id", event_id)["count"] == 1
        outbox_final = run_action(deployment.node_profile_env, "find-outbox-message", "--event-id", event_id)
        assert outbox_final["state"] == "delivered"


# --- N4: Central unavailable / Node offline operation ------------------------


def test_n4_central_unavailable_node_keeps_learning_and_syncs_after_recovery(tmp_path):
    """Desired final state: while Central is down, the Node HTTP learner
    flow (grading included) still works end to end, and the resulting
    Outbox row stays durable; once Central returns, a normal sync cycle
    delivers it. Forbidden invariant: any learner-facing failure while
    Central is down, or a lost/dropped event once Central returns."""
    with dual_deployment(tmp_path) as deployment:
        authored, package_path = author_and_package(
            deployment, prompt=_CONTENT["prompt"], options=_CONTENT["options"], correct_option_id="a"
        )
        import_and_activate(deployment, package_path)

        stop_process(deployment.central)
        try:
            # Node learning/grading is entirely local -- no call to
            # Central anywhere in this HTTP flow.
            _session, _attempt_url, submit_body = _submit_a_real_attempt(deployment, authored)
            assert submit_body["score"] == 1
            attempt_public_id = submit_body["attempt_public_id"]

            outbox = run_action(
                deployment.node_profile_env,
                "find-outbox-message-by-attempt",
                "--attempt-public-id",
                attempt_public_id,
            )
            assert outbox["found"] is True
            assert outbox["state"] == "pending"

            flush_result = run_sync_worker_once(deployment.node_profile_env)
            assert flush_result.returncode == 0
            assert "released_transient=1" in flush_result.stdout or "delivered=0" in flush_result.stdout

            outbox_still_pending = run_action(
                deployment.node_profile_env, "find-outbox-message", "--event-id", outbox["event_id"]
            )
            assert outbox_still_pending["state"] == "pending"
        finally:
            # Central "returns": a fresh Gunicorn process against the
            # SAME ephemeral Central database -- no data was lost, only
            # the process was down. A fresh process means a fresh
            # ephemeral loopback port (reserve_listener() binds port 0),
            # so the Node's own LINUXLAB_CENTRAL_URL must be updated to
            # match -- exactly the reconfiguration step a real restarted
            # Central behind a stable reverse-proxy URL would not need,
            # but this test's own bare-Gunicorn Central has no such
            # stable front door.
            central_listener = reserve_listener()
            restarted_central = start_gunicorn(
                deployment.central_profile_env, central_listener, deployment.tmp_dir / "central-restarted.log"
            )
            wait_ready(restarted_central, timeout_seconds=20)
            deployment.central = restarted_central
            deployment.node_profile_env = {
                **deployment.node_profile_env,
                "LINUXLAB_CENTRAL_URL": restarted_central.base_url,
            }

        flush_after_recovery = _flush_until_delivered(deployment.node_profile_env)
        assert flush_after_recovery is not None
        assert "delivered=1" in flush_after_recovery.stdout

        outbox_final = run_action(
            deployment.node_profile_env, "find-outbox-message", "--event-id", outbox["event_id"]
        )
        assert outbox_final["state"] == "delivered"


# --- N5: process restart ------------------------------------------------------


def test_n5_node_restart_preserves_active_package_and_pending_outbox(tmp_path):
    """Desired final state: after a Node process restart (same database,
    fresh Gunicorn process), the active package is still usable, the
    pending Outbox row survived, readiness recovers, and sync can resume.
    Forbidden invariant: the active package or the pending Outbox row is
    lost/reset by the restart."""
    with dual_deployment(tmp_path) as deployment:
        authored, package_path = author_and_package(
            deployment, prompt=_CONTENT["prompt"], options=_CONTENT["options"], correct_option_id="a"
        )
        import_and_activate(deployment, package_path)
        _session, _attempt_url, submit_body = _submit_a_real_attempt(deployment, authored)
        attempt_public_id = submit_body["attempt_public_id"]

        outbox_before = run_action(
            deployment.node_profile_env, "find-outbox-message-by-attempt", "--attempt-public-id", attempt_public_id
        )
        assert outbox_before["state"] == "pending"

        stop_process(deployment.node)

        node_listener = reserve_listener()
        restarted_node = start_gunicorn(
            deployment.node_profile_env, node_listener, deployment.tmp_dir / "node-restarted.log"
        )
        wait_ready(restarted_node, timeout_seconds=20)
        deployment.node = restarted_node

        # Readiness recovered and the active package is still usable via
        # a completely fresh HTTP session.
        fresh_session = http_bridge.LearnerSession.start(deployment.node.base_url)
        landing_status, landing_body = http_bridge.get_landing(
            fresh_session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
        )
        assert landing_status == 200
        assert _CONTENT["prompt"] in landing_body

        outbox_after_restart = run_action(
            deployment.node_profile_env, "find-outbox-message", "--event-id", outbox_before["event_id"]
        )
        assert outbox_after_restart["found"] is True
        assert outbox_after_restart["state"] == "pending"

        flush_result = run_sync_worker_once(deployment.node_profile_env)
        assert flush_result.returncode == 0
        assert "delivered=1" in flush_result.stdout

        outbox_final = run_action(
            deployment.node_profile_env, "find-outbox-message", "--event-id", outbox_before["event_id"]
        )
        assert outbox_final["state"] == "delivered"
