"""Task 15 Task 10: Central + Node backup/restore smoke (T15-D16, T15-D23),
using real `pg_dump`/`pg_restore` against real ephemeral PostgreSQL
databases -- never a scheduled backup service, never a bundled secrets+DB
artifact. A successful `pg_restore` alone is not treated as PASS: every
assertion below queries real application-level state through the same
release-gate action bridge Tasks 7-9 use, against the restored databases
specifically (POSTGRES_DB pointed at the `restore_central`/`restore_node`
gate database names, never the shared pytest test database or the
original pre-restore gate databases).
"""

import re

from scripts.release_gate.database import (
    create_database,
    drop_database,
    dump_database,
    restore_database,
)
from scripts.release_gate.e2e_support import (
    author_and_package,
    dual_deployment,
    import_and_activate,
    run_action,
    run_sync_worker_once,
)
from scripts.release_gate.http_bridge import (
    LearnerSession,
    get_attempt,
    get_landing,
    post_start,
    post_submit,
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


def _submit_one_attempt(deployment, authored):
    session = LearnerSession.start(deployment.node.base_url)
    get_landing(session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"])
    _status, attempt_url_path = post_start(
        session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
    )
    _status, attempt_body = get_attempt(session, attempt_url_path)
    question_public_id = _question_public_id_from_html(attempt_body)
    status, submit_body = post_submit(
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
    return submit_body


def _restore_and_verify(deployment, authored, tmp_path, dumps, first, second):
    """Restores both dumps into the already-created restore_central/
    restore_node databases and runs every application-level assertion.
    Caller owns creating/dropping those two databases."""
    central_dump_path, node_dump_path = dumps
    first_attempt_public_id, first_outbox = first
    second_attempt_public_id, second_outbox = second

    restore_database(deployment.db_admin, deployment.gate_names, "restore_central", central_dump_path)
    restore_database(deployment.db_admin, deployment.gate_names, "restore_node", node_dump_path)

    restored_central_env = {
        "DJANGO_SETTINGS_MODULE": "linuxlab.settings.central",
        "POSTGRES_DB": deployment.gate_names.restore_central,
    }
    restored_node_env_base = {
        "DJANGO_SETTINGS_MODULE": "linuxlab.settings.node",
        "POSTGRES_DB": deployment.gate_names.restore_node,
    }

    # --- Central minimum recoverable state ----------------------------------
    snapshot = run_action(
        restored_central_env,
        "find-publication-snapshot",
        "--source-public-id",
        authored["source_public_id"],
        "--package-version",
        str(authored["package_version"]),
    )
    assert snapshot["found"] is True
    assert snapshot["source_public_id"] == authored["source_public_id"]

    registration = run_action(restored_central_env, "find-node-registration", "--node-id", deployment.node_id)
    assert registration["found"] is True
    assert registration["enabled"] is True

    inbox = run_action(restored_central_env, "find-inbox-message", "--event-id", first_outbox["event_id"])
    assert inbox["found"] is True
    assert inbox["aggregate_public_id"] == first_attempt_public_id
    assert (
        run_action(restored_central_env, "count-inbox-messages", "--event-id", first_outbox["event_id"])["count"] == 1
    )

    # --- Node minimum recoverable state --------------------------------------
    active_package = run_action(
        restored_node_env_base,
        "find-active-package",
        "--package-type",
        "learning_path",
        "--source-public-id",
        authored["source_public_id"],
    )
    assert active_package["found"] is True
    assert active_package["state"] == "active"

    restored_first_attempt = run_action(
        restored_node_env_base, "find-attempt", "--attempt-public-id", first_attempt_public_id
    )
    assert restored_first_attempt["found"] is True
    assert restored_first_attempt["result_score"] == 1

    restored_second_attempt = run_action(
        restored_node_env_base, "find-attempt", "--attempt-public-id", second_attempt_public_id
    )
    assert restored_second_attempt["found"] is True
    assert restored_second_attempt["result_score"] == 1

    restored_pending_outbox = run_action(
        restored_node_env_base, "find-outbox-message", "--event-id", second_outbox["event_id"]
    )
    assert restored_pending_outbox["found"] is True
    assert restored_pending_outbox["state"] == "pending"

    # --- real Gunicorn against the restored databases; credentials
    # --- reattached from OUTSIDE the dump (this test's own already-issued
    # --- node_id/secret, never persisted inside either database dump) --------
    restored_central = None
    restored_node = None
    try:
        central_listener = reserve_listener()
        restored_central = start_gunicorn(restored_central_env, central_listener, tmp_path / "restored-central.log")
        wait_ready(restored_central, timeout_seconds=20)

        restored_node_env = {
            **restored_node_env_base,
            "LINUXLAB_SYNC_ENABLED": "true",
            "LINUXLAB_NODE_ID": deployment.node_id,
            "LINUXLAB_SYNC_SECRET": deployment.node_secret,
            "LINUXLAB_CENTRAL_URL": restored_central.base_url,
            "LINUXLAB_SYNC_ALLOW_INSECURE_HTTP": "true",
        }
        node_listener = reserve_listener()
        restored_node = start_gunicorn(restored_node_env, node_listener, tmp_path / "restored-node.log")
        wait_ready(restored_node, timeout_seconds=20)

        # Node readiness recovered, and the active installed content is
        # genuinely servable again over a fresh HTTP session.
        fresh_session = LearnerSession.start(restored_node.base_url)
        landing_status, landing_body = get_landing(
            fresh_session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
        )
        assert landing_status == 200
        assert _CONTENT["prompt"] in landing_body

        # Pending sync works after restore: the still-pending Outbox row
        # (attempt #2) delivers through the restored Node against the
        # restored Central.
        flush_after_restore = run_sync_worker_once(restored_node_env)
        assert flush_after_restore.returncode == 0
        assert "delivered=1" in flush_after_restore.stdout

        outbox_after_restore_sync = run_action(
            restored_node_env_base, "find-outbox-message", "--event-id", second_outbox["event_id"]
        )
        assert outbox_after_restore_sync["state"] == "delivered"

        inbox_after_restore_sync = run_action(
            restored_central_env, "find-inbox-message", "--event-id", second_outbox["event_id"]
        )
        assert inbox_after_restore_sync["found"] is True
    finally:
        if restored_node is not None:
            stop_process(restored_node)
        if restored_central is not None:
            stop_process(restored_central)


def test_central_and_node_backup_restore_smoke(tmp_path):
    with dual_deployment(tmp_path) as deployment:
        authored, package_path = author_and_package(
            deployment, prompt=_CONTENT["prompt"], options=_CONTENT["options"], correct_option_id="a"
        )
        import_and_activate(deployment, package_path)

        # Attempt #1: submitted AND synced before the dump, so Central's
        # durable Inbox already has real idempotency state to restore.
        first_submission = _submit_one_attempt(deployment, authored)
        first_attempt_public_id = first_submission["attempt_public_id"]
        first_outbox = run_action(
            deployment.node_profile_env,
            "find-outbox-message-by-attempt",
            "--attempt-public-id",
            first_attempt_public_id,
        )
        flush_result = run_sync_worker_once(deployment.node_profile_env)
        assert flush_result.returncode == 0
        assert "delivered=1" in flush_result.stdout

        # Attempt #2: submitted but deliberately NOT synced, so there is a
        # real pending Outbox row at dump time to prove survives restore.
        second_submission = _submit_one_attempt(deployment, authored)
        second_attempt_public_id = second_submission["attempt_public_id"]
        second_outbox = run_action(
            deployment.node_profile_env,
            "find-outbox-message-by-attempt",
            "--attempt-public-id",
            second_attempt_public_id,
        )
        assert second_outbox["state"] == "pending"

        # --- real pg_dump --------------------------------------------------------
        central_dump_path = tmp_path / "central.dump"
        node_dump_path = tmp_path / "node.dump"
        dump_database(deployment.db_admin, deployment.gate_names, "central", central_dump_path)
        dump_database(deployment.db_admin, deployment.gate_names, "node", node_dump_path)
        assert central_dump_path.stat().st_size > 0
        assert node_dump_path.stat().st_size > 0

        # --- real pg_restore, into FRESH disposable databases ---------------------
        create_database(deployment.db_admin, deployment.gate_names, "restore_central")
        create_database(deployment.db_admin, deployment.gate_names, "restore_node")
        try:
            _restore_and_verify(
                deployment,
                authored,
                tmp_path,
                (central_dump_path, node_dump_path),
                (first_attempt_public_id, first_outbox),
                (second_attempt_public_id, second_outbox),
            )
        finally:
            drop_database(deployment.db_admin, deployment.gate_names, "restore_node")
            drop_database(deployment.db_admin, deployment.gate_names, "restore_central")
