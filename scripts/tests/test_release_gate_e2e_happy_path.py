"""Task 15 Task 8: real dual-deployment happy-path E2E (T15-D02, T15-D09,
T15-D17, T15-D18, T15-D20, T15-D27).

Two genuinely separate ephemeral PostgreSQL databases, two real Gunicorn
processes (Central, Node), real HTTP throughout, an ephemeral Ed25519
signing key, a real signed `.llpkg`, the locked production
assessment/single_choice provider through the real production default
registries, a real issued Node sync credential, and the real
HttpSyncTransport delivering to Central's real /api/v1/sync/events/ --
no Django test-client substitute for the release scenario, no browser
automation.

Flow:
    Central: author+publish assessment/single_choice -> sign .llpkg
    Node:    verify -> import/stage -> activate
    HTTP:    GET landing -> POST start -> GET Attempt -> POST submit
             -> Result -> Progress -> activity.completed -> Outbox
    Sync:    HttpSyncTransport -> Central Inbox; duplicate delivery
             (a genuine resend of the same wire envelope, simulating a
             lost-ACK retry) remains idempotent.
"""

import re

from scripts.release_gate import http_bridge
from scripts.release_gate.e2e_support import (
    author_and_package,
    dual_deployment,
    import_and_activate,
    run_action,
    run_sync_worker_once,
)

_CONTENT = {
    "prompt": "Which command lists files?",
    "options": [{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
}


def _question_public_id_from_html(html):
    match = re.search(r'data-question-public-id="([0-9a-f-]{36})"', html)
    assert match, "no question_public_id found in rendered attempt page"
    return match.group(1)


def test_real_dual_deployment_happy_path(tmp_path):
    with dual_deployment(tmp_path) as deployment:
        # --- Central: author + publish + sign ------------------------------
        authored, package_path = author_and_package(
            deployment,
            prompt=_CONTENT["prompt"],
            options=_CONTENT["options"],
            correct_option_id="a",
        )

        # --- Node: verify + import/stage + activate -------------------------
        activated = import_and_activate(deployment, package_path)
        assert activated["state"] == "active"
        assert activated["source_public_id"] == authored["source_public_id"]
        assert activated["package_version"] == authored["package_version"]

        # --- Real HTTP learner flow ------------------------------------------
        session = http_bridge.LearnerSession.start(deployment.node.base_url)

        landing_status, landing_body = http_bridge.get_landing(
            session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
        )
        assert landing_status == 200
        assert _CONTENT["prompt"] in landing_body
        assert "answer_key" not in landing_body
        assert "correct_option_id" not in landing_body
        assert "scoring" not in landing_body

        start_status, attempt_url_path = http_bridge.post_start(
            session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
        )
        assert start_status == 303
        assert attempt_url_path

        attempt_get_status, attempt_body = http_bridge.get_attempt(session, attempt_url_path)
        assert attempt_get_status == 200
        assert _CONTENT["prompt"] in attempt_body
        assert "answer_key" not in attempt_body
        assert "correct_option_id" not in attempt_body
        assert "scoring" not in attempt_body

        question_public_id = _question_public_id_from_html(attempt_body)
        assert question_public_id == authored["question_public_id"]

        submit_status, submit_body = http_bridge.post_submit(
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
        assert submit_status == 200
        assert submit_body["score"] == 1
        assert submit_body["max_score"] == 1
        assert "answer_key" not in str(submit_body)
        assert "correct_option_id" not in str(submit_body)
        attempt_public_id = submit_body["attempt_public_id"]

        # --- Exact ActivityVersion/package provenance -------------------------
        attempt = run_action(deployment.node_profile_env, "find-attempt", "--attempt-public-id", attempt_public_id)
        assert attempt["found"] is True
        assert attempt["activity_version_id"] is None  # never the Central execution source
        assert attempt["installed_activity_public_id"] == authored["activity_public_id"]
        assert attempt["installed_activity_version"] == authored["activity_version"]
        assert attempt["installed_package_source_public_id"] == authored["source_public_id"]
        assert attempt["installed_package_version"] == authored["package_version"]
        assert attempt["result_score"] == 1
        assert attempt["result_max_score"] == 1

        # --- One logical Progress record ---------------------------------------
        progress = run_action(
            deployment.node_profile_env,
            "find-progress",
            "--learner-id",
            str(attempt["learner_id"]),
            "--activity-definition-public-id",
            authored["activity_public_id"],
        )
        assert progress["found"] is True
        assert progress["count"] == 1
        assert progress["completed"] is True
        assert progress["best_score"] == 1

        # --- Node Outbox: real activity.completed row, Task 14 v1 wire identity -
        # A domain event/Outbox row is enqueued transactionally with the
        # Result/Progress write, so it already exists before any sync
        # worker cycle runs. Found by aggregate_public_id (the Attempt's
        # own public_id, per the unversioned Task 14 v1 contract) since
        # the HTTP submit response never exposes a local event_id.
        outbox_before = run_action(
            deployment.node_profile_env, "find-outbox-message-by-attempt", "--attempt-public-id", attempt_public_id
        )
        assert outbox_before["found"] is True
        assert outbox_before["event_type"] == "activity.completed"
        assert outbox_before["aggregate_type"] == "attempt"
        assert outbox_before["aggregate_public_id"] == attempt_public_id
        assert outbox_before["state"] == "pending"
        event_id = outbox_before["event_id"]

        # --- Sync: real HttpSyncTransport delivery to Central's real Inbox -----
        flush_result = run_sync_worker_once(deployment.node_profile_env)
        assert flush_result.returncode == 0, flush_result.stdout + flush_result.stderr
        assert "claimed=1" in flush_result.stdout
        assert "delivered=1" in flush_result.stdout
        assert "auth_fault=False" in flush_result.stdout
        assert "protocol_fault=False" in flush_result.stdout

        outbox_after = run_action(
            deployment.node_profile_env, "find-outbox-message", "--event-id", event_id
        )
        assert outbox_after["found"] is True
        assert outbox_after["state"] == "delivered"

        inbox = run_action(deployment.central_profile_env, "find-inbox-message", "--event-id", event_id)
        assert inbox["found"] is True
        assert inbox["event_type"] == "activity.completed"
        assert inbox["aggregate_type"] == "attempt"
        assert inbox["aggregate_public_id"] == attempt_public_id
        assert run_action(deployment.central_profile_env, "count-inbox-messages", "--event-id", event_id)["count"] == 1

        # --- Duplicate delivery remains idempotent -------------------------------
        # A genuine resend of the SAME wire envelope (simulating a Node
        # retry after a lost ACK), through the real HttpSyncTransport
        # again -- never a second flush_outbox() cycle, since the row is
        # already DELIVERED and nothing would be claimed a second time.
        resend = run_action(
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
        assert resend["results"] == [{"event_id": event_id, "status": "duplicate", "code": ""}]
        assert run_action(deployment.central_profile_env, "count-inbox-messages", "--event-id", event_id)["count"] == 1
