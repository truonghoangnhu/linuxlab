"""Task 15 release-gate evidence schema, redaction, and failure model
(T15-D07; Task 11 corrective: canonical EvidenceV1, FailureRecord).
Pure-Python, database-free.
"""

import json
from unittest.mock import patch

import pytest

from scripts.release_gate.evidence import (
    EXIT_CODE,
    SCHEMA_VERSION,
    FailureRecord,
    RedactionKeyCollisionError,
    ReleaseEvidence,
    ScenarioResult,
    SecretLeakError,
    SecretRegistry,
    write_json_evidence,
    write_markdown_evidence,
)


def _make_evidence(**overrides):
    fields = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": "rabc123456789",
            "git_sha": "a" * 40,
            "started_at": "2026-08-31T00:00:00+00:00",
            "finished_at": "2026-08-31T00:05:00+00:00",
            "platform": "linux",
            "central_profile": "linuxlab.settings.central",
            "node_profile": "linuxlab.settings.node",
        },
        "validation": {
            "clean_tree": True,
            "central_check": "ok",
            "node_check": "ok",
            "central_migration_drift": "none",
            "node_migration_drift": "none",
        },
        "scenarios": {},
        "proofs": {},
        "retained_resources": {},
        "primary_failure": None,
        "cleanup_failure": None,
        "cleanup_status": "clean",
        "overall": "PASS",
        "exit_code": 0,
    }
    fields.update(overrides)
    return ReleaseEvidence(**fields)


# --- exit code mapping ---------------------------------------------------


def test_exit_code_mapping_is_exact():
    assert EXIT_CODE["PASS"] == 0
    assert EXIT_CODE["PRECHECK_FAILURE"] == 2
    assert EXIT_CODE["STARTUP_FAILURE"] == 3
    assert EXIT_CODE["SCENARIO_FAILURE"] == 4
    assert EXIT_CODE["CLEANUP_FAILURE"] == 5


# --- schema shape --------------------------------------------------------


def test_evidence_to_dict_has_required_schema_v1_shape():
    evidence = _make_evidence()

    doc = evidence.to_dict()

    assert doc["schema_version"] == 1
    assert doc["run"]["run_id"] == "rabc123456789"
    assert doc["run"]["git_sha"] == "a" * 40
    assert doc["run"]["central_profile"] == "linuxlab.settings.central"
    assert doc["run"]["node_profile"] == "linuxlab.settings.node"
    assert doc["overall"] in {"PASS", "PRECHECK_FAILURE", "STARTUP_FAILURE", "SCENARIO_FAILURE", "CLEANUP_FAILURE"}
    assert doc["exit_code"] == 0
    assert "validation" in doc
    assert "scenarios" in doc
    assert "proofs" in doc
    assert "retained_resources" in doc
    assert "primary_failure" in doc
    assert "cleanup_failure" in doc
    assert "cleanup_status" in doc


def test_evidence_validation_section_carries_real_check_status():
    evidence = _make_evidence(
        validation={
            "clean_tree": True,
            "central_check": "ok",
            "node_check": "ok",
            "central_migration_drift": "none",
            "node_migration_drift": "none",
        }
    )
    doc = evidence.to_dict()
    assert doc["validation"]["clean_tree"] is True
    assert doc["validation"]["central_check"] == "ok"
    assert doc["validation"]["node_check"] == "ok"
    assert doc["validation"]["central_migration_drift"] == "none"
    assert doc["validation"]["node_migration_drift"] == "none"


def test_scenario_result_carries_status_assertions_and_duration():
    result = ScenarioResult(
        name="invalid_package_signature",
        status="PASS",
        assertions={"rejected": True, "active_package_unchanged": True},
        duration_ms=42.5,
    )

    assert result.name == "invalid_package_signature"
    assert result.status == "PASS"
    assert result.assertions["rejected"] is True
    assert result.duration_ms == 42.5


# --- FailureRecord: the evidence security boundary ------------------------


def test_failure_record_carries_category_stage_code_and_safe_message():
    record = FailureRecord(
        category="SCENARIO_FAILURE",
        stage="n2_revoked_credential",
        code="AssertionError",
        safe_message="AssertionError raised during n2_revoked_credential",
    )
    assert record.category == "SCENARIO_FAILURE"
    assert record.stage == "n2_revoked_credential"
    assert record.code == "AssertionError"
    doc = record.to_dict()
    assert doc == {
        "category": "SCENARIO_FAILURE",
        "stage": "n2_revoked_credential",
        "code": "AssertionError",
        "safe_message": "AssertionError raised during n2_revoked_credential",
    }


def test_evidence_primary_and_cleanup_failure_are_failure_record_dicts():
    primary = FailureRecord(
        category="STARTUP_FAILURE", stage="n4_central_unavailable", code="StartupError", safe_message="x"
    )
    cleanup = FailureRecord(
        category="CLEANUP_FAILURE", stage="n4_central_unavailable", code="CleanupAggregateError", safe_message="y"
    )
    evidence = _make_evidence(
        overall="STARTUP_FAILURE",
        exit_code=3,
        primary_failure=primary.to_dict(),
        cleanup_failure=cleanup.to_dict(),
    )
    doc = evidence.to_dict()
    assert doc["primary_failure"]["category"] == "STARTUP_FAILURE"
    assert doc["cleanup_failure"]["category"] == "CLEANUP_FAILURE"


# --- secret redaction -------------------------------------------------------


def test_secret_registry_redacts_registered_value_from_text():
    secrets = SecretRegistry()
    secrets.register("NODE_SECRET_DO_NOT_LEAK")

    redacted = secrets.redact_text("bearer token: NODE_SECRET_DO_NOT_LEAK end")

    assert "NODE_SECRET_DO_NOT_LEAK" not in redacted
    assert "[REDACTED]" in redacted


def test_secret_registry_redacts_nested_dict_values():
    secrets = SecretRegistry()
    secrets.register("PRIVATE_KEY_DO_NOT_LEAK")

    redacted = secrets.redact_value(
        {"outer": {"inner": ["prefix PRIVATE_KEY_DO_NOT_LEAK suffix"]}}
    )

    assert "PRIVATE_KEY_DO_NOT_LEAK" not in json.dumps(redacted)


def test_secret_registry_ignores_empty_values():
    secrets = SecretRegistry()
    secrets.register("")
    secrets.register(None)

    # Must not raise and must not redact everything into oblivion.
    assert secrets.redact_text("hello world") == "hello world"


def test_write_json_evidence_redacts_secret_sentinels(tmp_path):
    secrets = SecretRegistry()
    secrets.register("NODE_SECRET_DO_NOT_LEAK")
    secrets.register("PRIVATE_KEY_DO_NOT_LEAK")
    evidence = _make_evidence(
        validation={"node_credential_issued": "NODE_SECRET_DO_NOT_LEAK"},
        proofs={"signing_key_pem": "PRIVATE_KEY_DO_NOT_LEAK"},
    )
    path = tmp_path / "release-evidence.json"

    write_json_evidence(path, evidence, secrets)

    text = path.read_text()
    assert "NODE_SECRET_DO_NOT_LEAK" not in text
    assert "PRIVATE_KEY_DO_NOT_LEAK" not in text
    assert json.loads(text)["schema_version"] == 1


# --- checkpoint corrective: secrets embedded in dict KEYS (finding #3) -----


def test_secret_registry_redacts_a_secret_embedded_in_a_dict_key():
    secrets = SecretRegistry()
    secrets.register("SUPER_SECRET_VALUE")

    redacted = secrets.redact_value({"node-SUPER_SECRET_VALUE-credential": {"inner": "ok"}})

    assert "SUPER_SECRET_VALUE" not in json.dumps(redacted)


def test_secret_registry_redacts_a_secret_that_is_exactly_the_whole_key():
    secrets = SecretRegistry()
    secrets.register("WHOLE_KEY_SECRET")

    redacted = secrets.redact_value({"WHOLE_KEY_SECRET": "value"})

    assert "WHOLE_KEY_SECRET" not in json.dumps(redacted)


def test_secret_registry_redacts_a_secret_in_a_nested_key_several_levels_deep():
    secrets = SecretRegistry()
    secrets.register("DEEP_KEY_SECRET")

    redacted = secrets.redact_value(
        {"outer": {"mid-DEEP_KEY_SECRET-mid": {"inner": [{"leaf-DEEP_KEY_SECRET": 1}]}}}
    )

    assert "DEEP_KEY_SECRET" not in json.dumps(redacted)


def test_secret_registry_redacts_overlapping_secrets_in_the_same_key():
    secrets = SecretRegistry()
    secrets.register("ABC")
    secrets.register("ABCDEF")

    redacted = secrets.redact_value({"key-ABCDEF-tail": "value"})

    assert "ABC" not in json.dumps(redacted)
    assert "DEF" not in json.dumps(redacted)


def test_secret_registry_fails_closed_on_redacted_key_collision():
    secrets = SecretRegistry()
    secrets.register("X")

    # "aXb" redacts to "a[REDACTED]b", which collides with a second key
    # that is already literally "a[REDACTED]b" -- two distinct original
    # keys must never silently collapse into one entry.
    with pytest.raises(RedactionKeyCollisionError):
        secrets.redact_value({"aXb": 1, "a[REDACTED]b": 2})


def test_secret_registry_does_not_raise_when_redaction_removes_a_would_be_duplicate_key_but_only_one_exists():
    secrets = SecretRegistry()
    secrets.register("X")

    redacted = secrets.redact_value({"aXb": 1, "unrelated": 2})

    assert "X" not in json.dumps(redacted)
    assert len(redacted) == 2


def test_write_json_evidence_redacts_a_secret_embedded_in_a_dict_key(tmp_path):
    secrets = SecretRegistry()
    secrets.register("KEY_EMBEDDED_SECRET")
    evidence = _make_evidence(validation={"node-KEY_EMBEDDED_SECRET-credential": "issued"})
    path = tmp_path / "release-evidence.json"

    write_json_evidence(path, evidence, secrets)

    assert "KEY_EMBEDDED_SECRET" not in path.read_text()


def test_secret_registry_assert_no_secrets_present_raises_when_a_secret_remains():
    secrets = SecretRegistry()
    secrets.register("LEAKED_SECRET")

    with pytest.raises(SecretLeakError):
        secrets.assert_no_secrets_present("this text still has LEAKED_SECRET in it")


def test_secret_registry_assert_no_secrets_present_passes_clean_text():
    secrets = SecretRegistry()
    secrets.register("LEAKED_SECRET")

    secrets.assert_no_secrets_present("this text is clean")  # does not raise


def test_write_json_evidence_aborts_and_leaves_no_artifact_if_a_secret_would_leak(
    tmp_path, monkeypatch
):
    import scripts.release_gate.evidence as evidence_module

    secrets = SecretRegistry()
    secrets.register("UNREDACTABLE_SECRET")
    evidence = _make_evidence(validation={"leak": "UNREDACTABLE_SECRET"})
    path = tmp_path / "release-evidence.json"

    # Simulate a redaction bug: value passes through untouched.
    monkeypatch.setattr(evidence_module.SecretRegistry, "redact_value", lambda self, value: value)

    with pytest.raises(SecretLeakError):
        write_json_evidence(path, evidence, secrets)

    assert not path.exists()


def test_write_markdown_evidence_redacts_secret_sentinels(tmp_path):
    secrets = SecretRegistry()
    secrets.register("NODE_SECRET_DO_NOT_LEAK")
    evidence = _make_evidence(validation={"node_credential_issued": "NODE_SECRET_DO_NOT_LEAK"})
    path = tmp_path / "release-evidence.md"

    write_markdown_evidence(path, evidence, secrets)

    text = path.read_text()
    assert "NODE_SECRET_DO_NOT_LEAK" not in text
    assert "# " in text  # some heading exists -- human-readable projection


# --- JSON / Markdown parity (Task 11 corrective, Important finding #3) -----


def test_markdown_evidence_includes_every_canonical_section():
    """Markdown is a projection of the SAME canonical EvidenceV1, not a
    separate partial schema -- it must surface every top-level section,
    not just scenarios/checks."""
    evidence = _make_evidence(
        run={
            "run_id": "rmdparity0001",
            "git_sha": "b" * 40,
            "started_at": "2026-09-04T00:00:00+00:00",
            "finished_at": "2026-09-04T00:05:00+00:00",
            "platform": "linux",
            "central_profile": "linuxlab.settings.central",
            "node_profile": "linuxlab.settings.node",
        },
        scenarios={
            "happy_path": ScenarioResult(name="happy_path", status="PASS", duration_ms=1.0).__dict__,
        },
        proofs={
            "package_sha256": "f" * 64,
            "payload_sha256": "e" * 64,
            "public_key_fingerprint": "d" * 64,
            "signature_verified": True,
            "outbox_to_inbox": "delivered",
        },
        retained_resources={"n2_revoked_credential": {"central_database": "linuxlab_gate_central_r1", "node_database": "linuxlab_gate_node_r1", "workspace_path": "/tmp/x"}},
    )
    path = _write_and_read_markdown(evidence)

    assert "rmdparity0001" in path
    assert "b" * 40 in path
    assert "happy_path" in path
    assert "f" * 64 in path  # package_sha256
    assert "d" * 64 in path  # public_key_fingerprint
    assert "signature_verified" in path.lower() or "true" in path.lower()
    assert "outbox" in path.lower()
    assert "linuxlab_gate_central_r1" in path
    assert "cleanup_status" in path.lower() or "clean" in path.lower()


def _write_and_read_markdown(evidence, tmp_path=None):
    import tempfile
    from pathlib import Path

    directory = Path(tmp_path) if tmp_path is not None else Path(tempfile.mkdtemp())
    path = directory / "release-evidence.md"
    write_markdown_evidence(path, evidence, SecretRegistry())
    return path.read_text()


# --- atomic writes -----------------------------------------------------


def test_write_json_evidence_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "release-evidence.json"

    write_json_evidence(path, _make_evidence(), SecretRegistry())

    remaining = list(tmp_path.iterdir())
    assert remaining == [path]


def test_write_json_evidence_does_not_leave_a_partial_file_on_failure(tmp_path):
    path = tmp_path / "release-evidence.json"

    with (
        patch("scripts.release_gate.evidence.json.dumps", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        write_json_evidence(path, _make_evidence(), SecretRegistry())

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_json_evidence_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "release-evidence.json"

    write_json_evidence(path, _make_evidence(), SecretRegistry())

    assert path.exists()


def test_write_json_evidence_overwrites_existing_file_atomically(tmp_path):
    path = tmp_path / "release-evidence.json"
    path.write_text("stale content")

    write_json_evidence(path, _make_evidence(overall="SCENARIO_FAILURE", exit_code=4), SecretRegistry())

    assert json.loads(path.read_text())["overall"] == "SCENARIO_FAILURE"
