"""Task 15 Task 11 corrective v9, Gap 1 (design spec Sections 4-10):
the canonical terminal ReleaseEvidence snapshot must be DEEPLY
immutable and fully detached from whatever mutable input structures
(scenario `assertions` dicts, `authority.__dict__`, etc.) were used to
build it.

Before this corrective, `ReleaseEvidence` was a plain (non-frozen)
dataclass whose fields were plain mutable dicts -- top-level
reassignment was never blocked, nested mutation was never blocked, and
`scenarios={result.name: result.__dict__ for result in results}`
aliased the SAME mutable `assertions` dict a scenario still held a
reference to, so a later mutation of that original dict silently
corrupted already-"finalized" evidence. `to_dict()` also returned the
SAME nested dict objects, so a caller mutating the returned dict
corrupted the canonical in-memory object too.

These are the mandatory RED tests from the v9 design spec, Section 35,
items 1-5.
"""

import json
from dataclasses import FrozenInstanceError

import pytest

from scripts.release_gate.evidence import (
    SCHEMA_VERSION,
    ReleaseEvidence,
    SecretRegistry,
    publish_evidence,
)


def _make_evidence(**overrides):
    fields = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": "rimmutable001",
            "git_sha": "a" * 40,
            "started_at": "2026-09-06T00:00:00+00:00",
            "finished_at": "2026-09-06T00:05:00+00:00",
            "authority": {"authoritative": True, "platform": "linux", "reason": None},
        },
        "environment": {
            "platform": "linux",
            "python_version": "3.12.0",
            "django_version": "5.2",
            "postgres_version": "180000",
        },
        "profiles": {"central": "linuxlab.settings.central", "node": "linuxlab.settings.node"},
        "validation": {
            "clean_tree": True,
            "central_check": "ok",
            "node_check": "ok",
            "central_migration_drift": "none",
            "node_migration_drift": "none",
        },
        "scenarios": {
            "happy_path": {
                "name": "happy_path",
                "status": "PASS",
                "assertions": {"package_installed_and_activated": True},
                "duration_ms": 1.0,
                "detail": "",
            }
        },
        "proofs": {
            "signature": {"verified": True},
            "outbox_to_inbox": {
                "event_uuid": "12345678-1234-5678-1234-567812345678",
                "inbox_count": 1,
                "node_state": "DELIVERED",
                "duplicate_idempotent": True,
            },
        },
        "retention": {
            "keep_on_failure_requested": False,
            "execution_resources_retained": False,
            "sanitized_debug_bundle_retained": True,
        },
        "primary_failure": None,
        "cleanup_failure": None,
        "finalization_failure": None,
        "cleanup_status": "clean",
        "overall": "PASS",
        "exit_code": 0,
    }
    fields.update(overrides)
    return ReleaseEvidence(**fields)


# --- RED test 1: top-level snapshot mutation rejected -----------------------


def test_top_level_field_reassignment_is_rejected():
    evidence = _make_evidence()
    with pytest.raises(FrozenInstanceError):
        evidence.retention = {"execution_resources_retained": False}


def test_top_level_overall_reassignment_is_rejected():
    evidence = _make_evidence()
    with pytest.raises(FrozenInstanceError):
        evidence.overall = "PASS"


# --- RED test 2: nested scenario/proof/validation/retention mutation -------


def test_nested_retention_item_mutation_is_rejected():
    evidence = _make_evidence()
    with pytest.raises(TypeError):
        evidence.retention["execution_resources_retained"] = True


def test_nested_scenario_assertions_mutation_is_rejected():
    evidence = _make_evidence()
    with pytest.raises(TypeError):
        evidence.scenarios["happy_path"]["assertions"]["package_installed_and_activated"] = False


def test_nested_proof_outbox_mutation_is_rejected():
    evidence = _make_evidence()
    with pytest.raises(TypeError):
        evidence.proofs["outbox_to_inbox"]["inbox_count"] = 2


def test_nested_validation_mutation_is_rejected():
    evidence = _make_evidence()
    with pytest.raises(TypeError):
        evidence.validation["clean_tree"] = False


def test_nested_run_authority_mutation_is_rejected():
    evidence = _make_evidence()
    with pytest.raises(TypeError):
        evidence.run["authority"]["authoritative"] = False


# --- RED test 3: original mutable input mutation does not affect snapshot --


def test_snapshot_detaches_from_original_run_dict():
    source_run = {
        "run_id": "rdetach00001",
        "git_sha": "b" * 40,
        "started_at": "2026-09-06T00:00:00+00:00",
        "finished_at": "2026-09-06T00:05:00+00:00",
        "authority": {"authoritative": True, "platform": "linux", "reason": None},
    }
    evidence = _make_evidence(run=source_run)
    source_run["run_id"] = "MUTATED"
    source_run["authority"]["authoritative"] = False

    assert evidence.run["run_id"] == "rdetach00001"
    assert evidence.run["authority"]["authoritative"] is True


def test_snapshot_detaches_from_original_scenario_assertions_dict():
    """The exact aliasing bug the spec describes: `ScenarioResult.
    __dict__` shares its `assertions` dict with whatever scenario code
    still holds a reference to it -- the snapshot must be built from an
    independent COPY, not the same object."""
    shared_assertions = {"package_installed_and_activated": True}
    scenarios = {
        "happy_path": {
            "name": "happy_path",
            "status": "PASS",
            "assertions": shared_assertions,
            "duration_ms": 1.0,
            "detail": "",
        }
    }
    evidence = _make_evidence(scenarios=scenarios)

    # Mutate the ORIGINAL dict the scenario code still references.
    shared_assertions["package_installed_and_activated"] = False

    assert evidence.scenarios["happy_path"]["assertions"]["package_installed_and_activated"] is True


def test_snapshot_detaches_from_original_retention_dict():
    source_retention = {
        "keep_on_failure_requested": False,
        "execution_resources_retained": False,
        "sanitized_debug_bundle_retained": True,
    }
    evidence = _make_evidence(retention=source_retention)
    source_retention["execution_resources_retained"] = True

    assert evidence.retention["execution_resources_retained"] is False


# --- RED test 4: renderer plain-data mutation does not affect snapshot -----


def test_to_dict_mutation_does_not_affect_snapshot():
    evidence = _make_evidence()
    doc = evidence.to_dict()

    doc["retention"]["execution_resources_retained"] = True
    doc["scenarios"]["happy_path"]["assertions"]["package_installed_and_activated"] = False
    doc["run"]["run_id"] = "MUTATED"

    assert evidence.retention["execution_resources_retained"] is False
    assert evidence.scenarios["happy_path"]["assertions"]["package_installed_and_activated"] is True
    assert evidence.run["run_id"] == "rimmutable001"


def test_to_dict_returns_a_fresh_plain_json_serializable_structure():
    evidence = _make_evidence()
    doc = evidence.to_dict()
    # Must be genuinely JSON-serializable plain data -- not
    # MappingProxyType/tuple wrappers escaping the renderer boundary.
    json.dumps(doc)
    assert type(doc) is dict
    assert type(doc["run"]) is dict
    assert type(doc["scenarios"]) is dict
    assert type(doc["scenarios"]["happy_path"]) is dict
    assert type(doc["scenarios"]["happy_path"]["assertions"]) is dict


# --- RED test 5: published permanence survives both original-input and ----
# --- returned-object mutation attempts --------------------------------------


def test_published_json_survives_original_input_and_returned_mutation_attempts(tmp_path):
    source_retention = {
        "keep_on_failure_requested": False,
        "execution_resources_retained": False,
        "sanitized_debug_bundle_retained": True,
    }
    evidence = _make_evidence(retention=source_retention)

    evidence_dir = tmp_path / "release-evidence"
    secrets = SecretRegistry()
    json_path, markdown_path = publish_evidence(evidence_dir, evidence, secrets)

    # Attempt to corrupt the original input dict AFTER publication.
    source_retention["execution_resources_retained"] = True

    # Attempt to corrupt the returned in-memory object.
    with pytest.raises(TypeError):
        evidence.retention["execution_resources_retained"] = True

    # Attempt to corrupt a freshly-rendered plain-data view.
    doc = evidence.to_dict()
    doc["retention"]["execution_resources_retained"] = True

    published_doc = json.loads(json_path.read_text())
    assert published_doc["retention"]["execution_resources_retained"] is False
    assert evidence.retention["execution_resources_retained"] is False
    assert "execution_resources_retained: False" in markdown_path.read_text()
