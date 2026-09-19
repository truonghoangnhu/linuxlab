"""Task 15 Task 11: the seven release-gate scenarios (happy path, N1-N5,
backup/restore smoke) as reusable functions, extracted verbatim from
the Task 8-10 pytest suite so both the pytest tests and
scripts/phase1_release_gate.py call the exact same real-infra logic --
never a second, drifted copy of the same business flow.

Every function takes a real `DualDeployment` (from
scripts.release_gate.e2e_support.dual_deployment()) already inside its
own `with` block, performs the real actions (real HTTP, real signed
.llpkg, real HttpSyncTransport, real pg_dump/pg_restore, the locked
production assessment/single_choice provider), asserts every invariant
the original test asserted, and returns a small evidence-safe `dict` of
non-secret facts on success (never a Node secret, private key material,
or answer-key content) -- or raises (an `AssertionError` from a failed
`assert`, or a real infrastructure exception) on failure, exactly as
the original pytest test would.
"""

import hashlib
import io
import re
import time
import zipfile
from pathlib import Path

import pytest

from linuxlab.publication.package import SIGNATURE_MEMBER
from scripts.release_gate import http_bridge
from scripts.release_gate.database import (
    create_database,
    dump_database,
    restore_database,
)
from scripts.release_gate.e2e_support import (
    ActionError,
    author_and_package,
    import_and_activate,
    run_action,
    run_sync_worker_once,
)
from scripts.release_gate.one_shot_proxy import OneShotResponseDroppingProxy
from scripts.release_gate.processes import (
    reserve_listener,
    restart_gunicorn,
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
    assert match, "no question_public_id found in rendered attempt page"
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


def _tamper_signature(package_path, *, deployment=None):
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
    # Task 15 Task 11 corrective v11, Section 22/26/27: reserved BEFORE
    # the write below (the actual tampered-package producer) -- the
    # tampered `.llpkg` was previously never individually ledger-owned
    # at all (v10's audit gap), only implicitly covered by the
    # enclosing workspace. Registered as the workspace's explicit
    # structural child, exactly like every other generated `.llpkg`.
    resource_ledger = getattr(deployment, "resource_ledger", None) if deployment is not None else None
    if resource_ledger is not None:
        resource_ledger.acquire(
            "temp_llpkg", str(tampered_path), parent_kind="workspace", parent_safe_id=str(deployment.tmp_dir)
        )
    tampered_path.write_bytes(buffer.getvalue())
    return tampered_path


def _public_key_fingerprint(deployment) -> str:
    """SHA-256 of the ephemeral Ed25519 PUBLIC key -- never the private
    key. Required release evidence content (design spec Section 17.1:
    "ephemeral signing public-key fingerprint")."""
    return hashlib.sha256(Path(deployment.public_key_path).read_bytes()).hexdigest()


def _submit_a_real_attempt(deployment, authored):
    """Shared setup for N2/N3/N4/N5: run the real learner HTTP flow once
    against an already-activated package, producing a real pending
    Outbox row to exercise sync behavior against."""
    session = http_bridge.LearnerSession.start(deployment.node.base_url)
    http_bridge.get_landing(
        session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
    )
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


# --- Happy path (Task 8) -----------------------------------------------------


def _measure_happy_path_idempotence(
    *, before_inbox_count: int, duplicate_response_observed: bool, after_inbox_count: int
) -> bool:
    """Task 15 Task 11 corrective v6, Section 11: durable idempotence
    proof. True only if the resend was recognized as a duplicate AND
    durable Central Inbox state never actually changed across it (the
    count measured before the resend equals the count measured AFTER
    it, and both equal 1) -- never merely the pre-resend count. A
    hypothetical bug that let a "duplicate" resend actually insert a
    second Inbox row (before=1, after=2) must be measured as NOT
    idempotent rather than silently reported as successful."""
    return duplicate_response_observed and before_inbox_count == 1 and after_inbox_count == 1


def run_happy_path(deployment) -> dict:
    authored, package_path = author_and_package(
        deployment,
        prompt=_CONTENT["prompt"],
        options=_CONTENT["options"],
        correct_option_id="a",
    )

    activated = import_and_activate(deployment, package_path)
    assert activated["state"] == "active"
    assert activated["source_public_id"] == authored["source_public_id"]
    assert activated["package_version"] == authored["package_version"]

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

    attempt = run_action(deployment.node_profile_env, "find-attempt", "--attempt-public-id", attempt_public_id)
    assert attempt["found"] is True
    assert attempt["activity_version_id"] is None
    assert attempt["installed_activity_public_id"] == authored["activity_public_id"]
    assert attempt["installed_activity_version"] == authored["activity_version"]
    assert attempt["installed_package_source_public_id"] == authored["source_public_id"]
    assert attempt["installed_package_version"] == authored["package_version"]
    assert attempt["result_score"] == 1
    assert attempt["result_max_score"] == 1

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

    outbox_before = run_action(
        deployment.node_profile_env, "find-outbox-message-by-attempt", "--attempt-public-id", attempt_public_id
    )
    assert outbox_before["found"] is True
    assert outbox_before["event_type"] == "activity.completed"
    assert outbox_before["aggregate_type"] == "attempt"
    assert outbox_before["aggregate_public_id"] == attempt_public_id
    assert outbox_before["state"] == "pending"
    event_id = outbox_before["event_id"]

    flush_result = run_sync_worker_once(deployment.node_profile_env)
    assert flush_result.returncode == 0, flush_result.stdout + flush_result.stderr
    assert "claimed=1" in flush_result.stdout
    assert "delivered=1" in flush_result.stdout
    assert "auth_fault=False" in flush_result.stdout
    assert "protocol_fault=False" in flush_result.stdout

    outbox_after = run_action(deployment.node_profile_env, "find-outbox-message", "--event-id", event_id)
    assert outbox_after["found"] is True
    # Task 15 Task 11 corrective v5, Section 8: MEASURED from the real
    # outbox row's own state, never a hard-coded "DELIVERED" literal --
    # so this stays truthful even under Python's -O mode, where the
    # `assert` just below disappears entirely.
    observed_node_state = "DELIVERED" if outbox_after["state"] == "delivered" else outbox_after["state"].upper()
    assert observed_node_state == "DELIVERED"

    inbox = run_action(deployment.central_profile_env, "find-inbox-message", "--event-id", event_id)
    assert inbox["found"] is True
    assert inbox["event_type"] == "activity.completed"
    assert inbox["aggregate_type"] == "attempt"
    assert inbox["aggregate_public_id"] == attempt_public_id
    # Task 15 Task 11 corrective v6, Section 11: measured BEFORE the
    # resend -- the durable idempotence proof below also requires the
    # count measured AFTER the resend, never only this pre-resend value.
    before_inbox_count = run_action(
        deployment.central_profile_env, "count-inbox-messages", "--event-id", event_id
    )["count"]
    assert before_inbox_count == 1

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
    # MEASURED from the real resend result -- the retry-driven resend of
    # an already-delivered event was recognized as the same event_id
    # (status "duplicate"), not treated as a fresh delivery.
    duplicate_response_observed = resend["results"] == [{"event_id": event_id, "status": "duplicate", "code": ""}]

    # Task 15 Task 11 corrective v6, Section 11: the DURABLE Central
    # Inbox state measured AFTER the resend -- this, not
    # before_inbox_count, is what the final proof reports. A resend
    # that the response claims was "duplicate" but that actually
    # mutated durable state (before=1, after=2) must still be caught.
    after_inbox_count = run_action(
        deployment.central_profile_env, "count-inbox-messages", "--event-id", event_id
    )["count"]
    observed_duplicate_idempotent = _measure_happy_path_idempotence(
        before_inbox_count=before_inbox_count,
        duplicate_response_observed=duplicate_response_observed,
        after_inbox_count=after_inbox_count,
    )
    assert observed_duplicate_idempotent

    # A successful import_and_activate() call earlier already performed
    # the real Ed25519 signature verification (Task 11/12's verify_
    # package() runs inside it) -- measured here as an explicit fact,
    # not assumed.
    observed_signature_verified = activated["state"] == "active"
    assert observed_signature_verified

    # Typed scenario assertions (Task 11 corrective v4, Section 19: MEASURED
    # from the same already-fetched real facts above, never a hard-coded
    # literal that would stay falsely successful under Python's -O mode,
    # where `assert` disappears entirely).
    package_installed_and_activated = (
        activated["state"] == "active"
        and activated["source_public_id"] == authored["source_public_id"]
        and activated["package_version"] == authored["package_version"]
    )
    assert package_installed_and_activated

    result_matches_installed_artifact = (
        attempt["found"] is True
        and attempt["installed_activity_public_id"] == authored["activity_public_id"]
        and attempt["installed_package_source_public_id"] == authored["source_public_id"]
        and attempt["installed_package_version"] == authored["package_version"]
        and attempt["result_score"] == 1
    )
    assert result_matches_installed_artifact

    completion_event_created = (
        outbox_before["found"] is True and outbox_before["event_type"] == "activity.completed"
    )
    assert completion_event_created

    event_delivered_to_central = outbox_after["state"] == "delivered" and inbox["found"] is True
    assert event_delivered_to_central

    return {
        "event_id": event_id,
        "attempt_public_id": attempt_public_id,
        "score": submit_body["score"],
        "max_score": submit_body["max_score"],
        "package_sha256": hashlib.sha256(package_path.read_bytes()).hexdigest(),
        "public_key_fingerprint": _public_key_fingerprint(deployment),
        # Task 15 Task 11 corrective v5/v6, Section 8/11: MEASURED
        # values (observed_signature_verified/after_inbox_count/
        # observed_node_state/observed_duplicate_idempotent, computed
        # above from real facts), never hard-coded literals -- so
        # evidence stays truthful even under Python's -O mode, where
        # every `assert` above disappears entirely.
        "signature_verified": observed_signature_verified,
        # The delivered-Outbox / found-Inbox / exactly-one-Inbox-row /
        # resend-is-duplicate sequence already measured above IS the
        # real Outbox->Inbox delivery proof for this run -- reported as
        # a typed structure (Task 11 corrective v2, Section 7.3), never
        # a bare "delivered" string.
        "outbox_to_inbox": {
            "event_uuid": event_id,
            # Task 15 Task 11 corrective v6, Section 11: the DURABLE
            # post-resend count, never the pre-resend one.
            "inbox_count": after_inbox_count,
            "node_state": observed_node_state,
            "duplicate_idempotent": observed_duplicate_idempotent,
        },
        "package_installed_and_activated": package_installed_and_activated,
        "result_matches_installed_artifact": result_matches_installed_artifact,
        "completion_event_created": completion_event_created,
        "event_delivered_to_central": event_delivered_to_central,
    }


# --- N1: invalid signature ----------------------------------------------------


def run_n1_tampered_replacement(deployment) -> dict:
    authored_a, package_a_path = author_and_package(
        deployment, prompt=_CONTENT["prompt"], options=_CONTENT["options"], correct_option_id="a"
    )
    activated_a = import_and_activate(deployment, package_a_path)
    assert activated_a["state"] == "active"

    def _active_a():
        return run_action(
            deployment.node_profile_env,
            "find-active-package",
            "--package-type",
            "learning_path",
            "--source-public-id",
            authored_a["source_public_id"],
        )

    def _inventory_count():
        return run_action(
            deployment.node_profile_env,
            "count-installed-packages",
            "--package-type",
            "learning_path",
            "--source-public-id",
            authored_a["source_public_id"],
        )["count"]

    active_before = _active_a()
    assert active_before["found"] is True
    assert active_before["package_version"] == authored_a["package_version"]
    assert active_before["state"] == "active"
    payload_sha256_before = active_before["payload_sha256"]
    assert _inventory_count() == 1

    session = http_bridge.LearnerSession.start(deployment.node.base_url)
    landing_status_before, landing_body_before = http_bridge.get_landing(
        session, authored_a["source_public_id"], authored_a["activity_public_id"], authored_a["activity_version"]
    )
    assert landing_status_before == 200
    assert _CONTENT["prompt"] in landing_body_before

    republished_b = run_action(
        deployment.central_profile_env, "republish", "--source-public-id", authored_a["source_public_id"]
    )
    assert republished_b["package_version"] != authored_a["package_version"]

    package_b_path = (
        deployment.tmp_dir
        / f"package-b-{republished_b['source_public_id']}-v{republished_b['package_version']}.llpkg"
    )
    # Task 15 Task 11 corrective v10, Section 15 (hardened by corrective
    # v11, Section 22/27): reserved BEFORE the build-and-sign-package
    # action (the actual .llpkg producer) is invoked, mirroring
    # e2e_support.author_and_package()'s own acquisition of its package,
    # and registered as the workspace's explicit structural child
    # (corrective v11, Section 13/15).
    resource_ledger = getattr(deployment, "resource_ledger", None)
    if resource_ledger is not None:
        resource_ledger.acquire(
            "temp_llpkg", str(package_b_path), parent_kind="workspace", parent_safe_id=str(deployment.tmp_dir)
        )
    run_action(
        deployment.central_profile_env,
        "build-and-sign-package",
        "--source-public-id",
        republished_b["source_public_id"],
        "--package-version",
        str(republished_b["package_version"]),
        "--private-key-file",
        str(deployment.private_key_path),
        "--output-file",
        str(package_b_path),
    )
    tampered_b_path = _tamper_signature(package_b_path, deployment=deployment)

    with pytest.raises(ActionError, match="PackageImportError") as exc_info:
        import_and_activate(deployment, tampered_b_path)
    replacement_rejected = "PackageImportError" in str(exc_info.value)
    assert replacement_rejected

    active_after = _active_a()
    assert active_after["found"] is True
    assert active_after["package_version"] == authored_a["package_version"]
    assert active_after["payload_sha256"] == payload_sha256_before
    assert active_after["state"] == "active"
    assert _inventory_count() == 1

    landing_status_after, landing_body_after = http_bridge.get_landing(
        session, authored_a["source_public_id"], authored_a["activity_public_id"], authored_a["activity_version"]
    )
    assert landing_status_after == 200
    assert _CONTENT["prompt"] in landing_body_after

    # Typed scenario assertions (Task 11 corrective v4, Section 19: MEASURED
    # from the same already-fetched real facts above).
    existing_active_preserved = active_after["found"] is True and active_after["state"] == "active"
    assert existing_active_preserved

    active_package_identity_unchanged = (
        active_after["package_version"] == authored_a["package_version"]
        and active_after["payload_sha256"] == payload_sha256_before
    )
    assert active_package_identity_unchanged

    return {
        "active_package_version": authored_a["package_version"],
        "payload_sha256": payload_sha256_before,
        "rejected_replacement_version": republished_b["package_version"],
        "public_key_fingerprint": _public_key_fingerprint(deployment),
        "existing_active_preserved": existing_active_preserved,
        "active_package_identity_unchanged": active_package_identity_unchanged,
        "replacement_rejected": replacement_rejected,
    }


# --- N2: invalid/revoked credential ------------------------------------------


def run_n2_revoked_credential(deployment) -> dict:
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
        deployment.node_profile_env, "find-outbox-message", "--event-id", outbox_before["event_id"]
    )
    # Corrective B Task 8 §25: a revoked credential is a channel/
    # credential fault, now immediately terminal -- the attempted row
    # ends in dead_letter rather than returning to pending for automatic
    # auth retry.
    assert outbox_after["state"] == "dead_letter"

    central_inbox_count = run_action(
        deployment.central_profile_env, "count-inbox-messages", "--event-id", outbox_before["event_id"]
    )["count"]
    assert central_inbox_count == 0

    # Typed scenario assertions (Task 11 corrective v4, Section 19: MEASURED
    # from the same already-fetched real facts above).
    request_rejected = "auth_fault=True" in flush_result.stdout and "delivered=0" in flush_result.stdout
    assert request_rejected

    central_inbox_mutated = central_inbox_count != 0
    assert not central_inbox_mutated

    revoked_credential_not_accepted = outbox_after["state"] == "dead_letter"
    assert revoked_credential_not_accepted

    return {
        "event_id": outbox_before["event_id"],
        "request_rejected": request_rejected,
        "central_inbox_mutated": central_inbox_mutated,
        "revoked_credential_not_accepted": revoked_credential_not_accepted,
    }


# --- N3: ACK lost after durable Central Inbox --------------------------------


def run_n3_ack_lost_real_proxy(deployment) -> dict:
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

    proxy_listener = reserve_listener()
    proxy = OneShotResponseDroppingProxy(
        proxy_listener, deployment.central.listener_host, deployment.central.listener_port
    )
    proxy.start()
    try:
        node_env_via_proxy = {**deployment.node_profile_env, "LINUXLAB_CENTRAL_URL": proxy.base_url}
        first_flush = run_sync_worker_once(node_env_via_proxy)
        assert first_flush.returncode == 0, first_flush.stdout + first_flush.stderr
        assert "claimed=1" in first_flush.stdout
        assert "released_transient=1" in first_flush.stdout or "delivered=0" in first_flush.stdout
    finally:
        proxy.stop()

    inbox_count_before_retry = run_action(
        deployment.central_profile_env, "count-inbox-messages", "--event-id", event_id
    )["count"]
    assert inbox_count_before_retry == 1

    outbox_still_pending = run_action(deployment.node_profile_env, "find-outbox-message", "--event-id", event_id)
    assert outbox_still_pending["state"] == "pending"

    flush_after_retry = _flush_until_delivered(deployment.node_profile_env)
    assert flush_after_retry is not None
    assert "delivered=1" in flush_after_retry.stdout

    final_inbox_count = run_action(
        deployment.central_profile_env, "count-inbox-messages", "--event-id", event_id
    )["count"]
    assert final_inbox_count == 1
    outbox_final = run_action(deployment.node_profile_env, "find-outbox-message", "--event-id", event_id)
    assert outbox_final["state"] == "delivered"

    # Typed scenario assertions (Task 11 corrective v4, Section 19: MEASURED
    # from the same already-fetched real facts above).
    central_commit_before_ack_loss = inbox_count_before_retry == 1
    assert central_commit_before_ack_loss

    node_pending_before_retry = outbox_still_pending["state"] == "pending"
    assert node_pending_before_retry

    final_node_state = "DELIVERED" if outbox_final["state"] == "delivered" else outbox_final["state"].upper()
    assert final_node_state == "DELIVERED"

    # The retry-driven redelivery landed on the SAME Inbox count (1,
    # not 2) as before the retry -- that IS the idempotence proof: the
    # retry was recognized as the same event_id, not treated as a fresh
    # duplicate event.
    duplicate_idempotent = final_inbox_count == inbox_count_before_retry == 1
    assert duplicate_idempotent

    return {
        "event_id": event_id,
        "central_commit_before_ack_loss": central_commit_before_ack_loss,
        "node_pending_before_retry": node_pending_before_retry,
        "final_inbox_count": final_inbox_count,
        "final_node_state": final_node_state,
        "duplicate_idempotent": duplicate_idempotent,
    }


# --- N4: Central unavailable / Node offline operation ------------------------


def run_n4_central_unavailable(deployment) -> dict:
    authored, package_path = author_and_package(
        deployment, prompt=_CONTENT["prompt"], options=_CONTENT["options"], correct_option_id="a"
    )
    import_and_activate(deployment, package_path)

    stop_process(deployment.central)
    outbox = None
    try:
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
        def _register_restarted_central(new_managed):
            deployment.central = new_managed
            deployment.node_profile_env = {
                **deployment.node_profile_env,
                "LINUXLAB_CENTRAL_URL": new_managed.base_url,
            }

        central_restarted_log_path = deployment.tmp_dir / "central-restarted.log"
        # Task 15 Task 11 corrective v11, Section 26/27: reserved BEFORE
        # restart_gunicorn() (the actual log-file producer) is called --
        # this restart log was never individually ledger-owned at all
        # before v11 (audit gap identified by the v10 review).
        resource_ledger = getattr(deployment, "resource_ledger", None)
        if resource_ledger is not None:
            resource_ledger.acquire(
                "raw_runtime_log",
                str(central_restarted_log_path),
                parent_kind="workspace",
                parent_safe_id=str(deployment.tmp_dir),
            )
        restart_gunicorn(
            deployment.central,
            deployment.central_profile_env,
            central_restarted_log_path,
            timeout_seconds=20,
            on_replacement_started=_register_restarted_central,
        )

    flush_after_recovery = _flush_until_delivered(deployment.node_profile_env)
    assert flush_after_recovery is not None
    assert "delivered=1" in flush_after_recovery.stdout

    outbox_final = run_action(deployment.node_profile_env, "find-outbox-message", "--event-id", outbox["event_id"])
    assert outbox_final["state"] == "delivered"

    # Typed scenario assertions (Task 11 corrective v4, Section 19: MEASURED
    # from the same already-fetched real facts above).
    local_runtime_available = submit_body["score"] == 1 and outbox["found"] is True
    assert local_runtime_available

    outbox_remained_durable = outbox_still_pending["state"] == "pending"
    assert outbox_remained_durable

    recovery_delivery_success = outbox_final["state"] == "delivered"
    assert recovery_delivery_success

    # The SAME event_id (never a freshly created one) is what was
    # queried throughout, and it is this exact query that just
    # confirmed "delivered" above -- that IS the no-loss proof.
    no_event_loss = outbox_final["state"] == "delivered"
    assert no_event_loss

    return {
        "event_id": outbox["event_id"],
        "local_runtime_available": local_runtime_available,
        "outbox_remained_durable": outbox_remained_durable,
        "no_event_loss": no_event_loss,
        "recovery_delivery_success": recovery_delivery_success,
    }


# --- N5: process restart ------------------------------------------------------


def run_n5_node_restart(deployment) -> dict:
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

    def _register_restarted_node(new_managed):
        deployment.node = new_managed

    node_restarted_log_path = deployment.tmp_dir / "node-restarted.log"
    # Task 15 Task 11 corrective v11, Section 26/27: reserved BEFORE
    # restart_gunicorn() (the actual log-file producer) is called.
    resource_ledger = getattr(deployment, "resource_ledger", None)
    if resource_ledger is not None:
        resource_ledger.acquire(
            "raw_runtime_log",
            str(node_restarted_log_path),
            parent_kind="workspace",
            parent_safe_id=str(deployment.tmp_dir),
        )
    restart_gunicorn(
        deployment.node,
        deployment.node_profile_env,
        node_restarted_log_path,
        timeout_seconds=20,
        on_replacement_started=_register_restarted_node,
    )

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

    # Typed scenario assertions (Task 11 corrective v4, Section 19: MEASURED
    # from the same already-fetched real facts above).
    active_package_survived_restart = landing_status == 200 and _CONTENT["prompt"] in landing_body
    assert active_package_survived_restart

    pending_outbox_survived_restart = (
        outbox_after_restart["found"] is True and outbox_after_restart["state"] == "pending"
    )
    assert pending_outbox_survived_restart

    runtime_recovered = landing_status == 200
    assert runtime_recovered

    delivery_resumed = outbox_final["state"] == "delivered"
    assert delivery_resumed

    return {
        "event_id": outbox_before["event_id"],
        "active_package_survived_restart": active_package_survived_restart,
        "pending_outbox_survived_restart": pending_outbox_survived_restart,
        "runtime_recovered": runtime_recovered,
        "delivery_resumed": delivery_resumed,
    }


# --- Backup/restore smoke (Task 10) -------------------------------------------


def _submit_one_attempt(deployment, authored):
    session = http_bridge.LearnerSession.start(deployment.node.base_url)
    http_bridge.get_landing(
        session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
    )
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
    return submit_body


def _restore_and_verify(deployment, authored, workspace, dumps, first, second):
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
    assert registration["state"] == "active"

    inbox = run_action(restored_central_env, "find-inbox-message", "--event-id", first_outbox["event_id"])
    assert inbox["found"] is True
    assert inbox["aggregate_public_id"] == first_attempt_public_id
    assert (
        run_action(restored_central_env, "count-inbox-messages", "--event-id", first_outbox["event_id"])["count"] == 1
    )

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

    resource_ledger = getattr(deployment, "resource_ledger", None)

    restored_central = None
    restored_node = None
    try:
        central_listener = reserve_listener()
        restored_central_log_path = workspace / "restored-central.log"
        # Task 15 Task 11 corrective v11, Section 26/27: reserved BEFORE
        # start_gunicorn() (the actual log-file producer) is called --
        # these restored logs were never individually ledger-owned at
        # all before v11 (audit gap identified by the v10 review).
        if resource_ledger is not None:
            resource_ledger.acquire(
                "raw_runtime_log",
                str(restored_central_log_path),
                parent_kind="workspace",
                parent_safe_id=str(workspace),
            )
        restored_central = start_gunicorn(restored_central_env, central_listener, restored_central_log_path)
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
        restored_node_log_path = workspace / "restored-node.log"
        if resource_ledger is not None:
            resource_ledger.acquire(
                "raw_runtime_log",
                str(restored_node_log_path),
                parent_kind="workspace",
                parent_safe_id=str(workspace),
            )
        restored_node = start_gunicorn(restored_node_env, node_listener, restored_node_log_path)
        wait_ready(restored_node, timeout_seconds=20)

        fresh_session = http_bridge.LearnerSession.start(restored_node.base_url)
        landing_status, landing_body = http_bridge.get_landing(
            fresh_session, authored["source_public_id"], authored["activity_public_id"], authored["activity_version"]
        )
        assert landing_status == 200
        assert _CONTENT["prompt"] in landing_body

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

    # Typed scenario assertions (Task 11 corrective v4, Section 19: MEASURED
    # from the same already-fetched real facts above, never a hard-coded
    # literal).
    central_business_state_restored = (
        snapshot["found"] is True
        and snapshot["source_public_id"] == authored["source_public_id"]
        and registration["found"] is True
        and registration["state"] == "active"
        and inbox["found"] is True
    )
    assert central_business_state_restored

    node_business_state_restored = (
        active_package["found"] is True
        and active_package["state"] == "active"
        and restored_first_attempt["found"] is True
        and restored_first_attempt["result_score"] == 1
        and restored_second_attempt["found"] is True
        and restored_second_attempt["result_score"] == 1
    )
    assert node_business_state_restored

    pending_outbox_restored = restored_pending_outbox["found"] is True and restored_pending_outbox["state"] == "pending"
    assert pending_outbox_restored

    restored_sync_can_resume = (
        outbox_after_restore_sync["state"] == "delivered" and inbox_after_restore_sync["found"] is True
    )
    assert restored_sync_can_resume

    return {
        "central_business_state_restored": central_business_state_restored,
        "node_business_state_restored": node_business_state_restored,
        "pending_outbox_restored": pending_outbox_restored,
        "restored_sync_can_resume": restored_sync_can_resume,
    }


def run_backup_restore_smoke(deployment) -> dict:
    authored, package_path = author_and_package(
        deployment, prompt=_CONTENT["prompt"], options=_CONTENT["options"], correct_option_id="a"
    )
    import_and_activate(deployment, package_path)

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

    second_submission = _submit_one_attempt(deployment, authored)
    second_attempt_public_id = second_submission["attempt_public_id"]
    second_outbox = run_action(
        deployment.node_profile_env,
        "find-outbox-message-by-attempt",
        "--attempt-public-id",
        second_attempt_public_id,
    )
    assert second_outbox["state"] == "pending"

    # Task 15 Task 11 corrective v8, Section 9-11 (resource_ledger lookup
    # itself hoisted here by corrective v10, Section 15, and further by
    # corrective v11, Section 22/27 so each dump's reservation below can
    # use it too): `resource_ledger` is attached by gate_runner.
    # _run_one_scenario() (never required -- calling this scenario
    # directly, as scripts.tests.test_release_gate_backup_restore.py's
    # own baseline test does, is still fully supported without a ledger
    # present).
    #
    # Disposal of the two restore databases acquired further below is
    # NOT this scenario's own responsibility any more (Section 15/16:
    # "restore disposal must execute under lifecycle stage CLEANUP") --
    # see gate_runner._run_one_scenario()'s own restore-database cleanup
    # step, which always runs after this function returns OR raises,
    # regardless of outcome.
    resource_ledger = getattr(deployment, "resource_ledger", None)

    central_dump_path = deployment.tmp_dir / "central.dump"
    node_dump_path = deployment.tmp_dir / "node.dump"
    # Task 15 Task 11 corrective v10, Section 15 (hardened by corrective
    # v11, Section 22-27): each dump's reservation is acquired BEFORE
    # its own dump_database() call (the actual pg_dump producer) --
    # never only after BOTH succeed -- so a partial dump file left by a
    # producer that raises partway through is already ledger-owned,
    # closing the partial-write window. Registered as the workspace's
    # explicit structural child (corrective v11, Section 13/15).
    if resource_ledger is not None:
        resource_ledger.acquire(
            "temp_dump", str(central_dump_path), parent_kind="workspace", parent_safe_id=str(deployment.tmp_dir)
        )
    dump_database(deployment.db_admin, deployment.gate_names, "central", central_dump_path)
    assert central_dump_path.stat().st_size > 0

    if resource_ledger is not None:
        resource_ledger.acquire(
            "temp_dump", str(node_dump_path), parent_kind="workspace", parent_safe_id=str(deployment.tmp_dir)
        )
    dump_database(deployment.db_admin, deployment.gate_names, "node", node_dump_path)
    assert node_dump_path.stat().st_size > 0

    # Task 15 Task 11 corrective v8, Section 9-11: acquire ownership of
    # each restore database in the shared resource-ownership ledger
    # IMMEDIATELY once it is successfully created -- restore_central is
    # already ledger-owned before restore_node is even attempted, so a
    # partial-creation failure (restore_node's create raising after
    # restore_central's succeeded) can never leave a physically-created
    # database the ledger doesn't yet know about.
    create_database(deployment.db_admin, deployment.gate_names, "restore_central")
    if resource_ledger is not None:
        resource_ledger.acquire("restore_central_db", deployment.gate_names.restore_central)

    create_database(deployment.db_admin, deployment.gate_names, "restore_node")
    if resource_ledger is not None:
        resource_ledger.acquire("restore_node_db", deployment.gate_names.restore_node)

    restore_assertions = _restore_and_verify(
        deployment,
        authored,
        deployment.tmp_dir,
        (central_dump_path, node_dump_path),
        (first_attempt_public_id, first_outbox),
        (second_attempt_public_id, second_outbox),
    )

    return {
        "first_attempt_public_id": first_attempt_public_id,
        "second_attempt_public_id": second_attempt_public_id,
        "central_dump_bytes": central_dump_path.stat().st_size,
        "node_dump_bytes": node_dump_path.stat().st_size,
        **restore_assertions,
    }
