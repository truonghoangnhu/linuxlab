"""Task 15 Task 11 corrective v2, Section 13 ("Transactional evidence
publication") and Section 18.2 ("Evidence publication failures"):
publication of the JSON+Markdown evidence pair is all-or-nothing. Both
renderings are built and secret-scanned in memory before anything
touches the final directory; both are written into a private staging
directory; the staging directory is atomically published as the final
evidence directory. A pre-existing final directory is a fail-closed
collision, never an overwrite. Any failure removes staging and leaves
no partial pair at the final location.
"""

from unittest.mock import patch

import pytest

from scripts.release_gate.environment import Authority
from scripts.release_gate.evidence import (
    SCHEMA_VERSION,
    EvidencePublicationError,
    ReleaseEvidence,
    SecretLeakError,
    SecretRegistry,
    publish_evidence,
)


def _make_evidence(**overrides):
    fields = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": "rpub00000001",
            "git_sha": "a" * 40,
            "started_at": "2026-09-04T00:00:00+00:00",
            "finished_at": "2026-09-04T00:05:00+00:00",
            "authority": Authority(authoritative=True, platform="linux", reason=None).__dict__,
        },
        "environment": {"platform": "linux", "python_version": "3.12.0", "django_version": "5.2", "postgres_version": "180000"},
        "profiles": {"central": "linuxlab.settings.central", "node": "linuxlab.settings.node"},
        "validation": {
            "clean_tree": True,
            "central_check": "ok",
            "node_check": "ok",
            "central_migration_drift": "none",
            "node_migration_drift": "none",
        },
        "scenarios": {},
        "proofs": {},
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


def test_publish_evidence_writes_both_files_atomically(tmp_path):
    evidence_dir = tmp_path / "artifacts" / "phase1-release" / "rpub00000001"

    json_path, markdown_path = publish_evidence(evidence_dir, _make_evidence(), SecretRegistry())

    assert json_path == evidence_dir / "release-evidence.json"
    assert markdown_path == evidence_dir / "release-evidence.md"
    assert json_path.exists()
    assert markdown_path.exists()
    # No staging leftovers beside the final published directory.
    assert sorted(p.name for p in evidence_dir.parent.iterdir()) == ["rpub00000001"]


def test_publish_evidence_fails_closed_on_existing_directory(tmp_path):
    evidence_dir = tmp_path / "rpub00000001"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "release-evidence.json").write_text('{"stale": true}')

    with pytest.raises(EvidencePublicationError):
        publish_evidence(evidence_dir, _make_evidence(), SecretRegistry())

    # The pre-existing file must never be overwritten.
    assert (evidence_dir / "release-evidence.json").read_text() == '{"stale": true}'


def test_publish_evidence_aborts_and_leaves_nothing_on_secret_leak(tmp_path):
    evidence_dir = tmp_path / "rpub00000001"
    secrets = SecretRegistry()
    secrets.register("UNREDACTABLE_SECRET")
    evidence = _make_evidence(validation={"leak": "UNREDACTABLE_SECRET"})

    import scripts.release_gate.evidence as evidence_module

    with (
        patch.object(evidence_module.SecretRegistry, "redact_value", lambda self, value: value),
        pytest.raises(SecretLeakError),
    ):
        publish_evidence(evidence_dir, evidence, secrets)

    assert not evidence_dir.exists()
    # No staging directory left behind either.
    assert list(tmp_path.iterdir()) == []


def test_publish_evidence_leaves_no_partial_pair_when_markdown_write_fails(tmp_path):
    evidence_dir = tmp_path / "rpub00000001"

    import scripts.release_gate.evidence as evidence_module

    original_atomic_write = evidence_module._atomic_write_text
    calls = {"n": 0}

    def _flaky_write(path, text):
        calls["n"] += 1
        if calls["n"] == 2:  # second write (markdown) fails
            raise RuntimeError("simulated disk failure")
        return original_atomic_write(path, text)

    with (
        patch.object(evidence_module, "_atomic_write_text", side_effect=_flaky_write),
        pytest.raises(RuntimeError),
    ):
        publish_evidence(evidence_dir, _make_evidence(), SecretRegistry())

    assert not evidence_dir.exists()
    assert list(tmp_path.iterdir()) == []


def test_publish_evidence_leaves_no_partial_pair_when_atomic_rename_fails(tmp_path):
    evidence_dir = tmp_path / "rpub00000001"

    import scripts.release_gate.evidence as evidence_module

    with (
        patch.object(evidence_module.os, "rename", side_effect=OSError("simulated rename failure")),
        pytest.raises(OSError),
    ):
        publish_evidence(evidence_dir, _make_evidence(), SecretRegistry())

    assert not evidence_dir.exists()
    assert list(tmp_path.iterdir()) == []


def test_publish_evidence_content_round_trips(tmp_path):
    import json

    evidence_dir = tmp_path / "rpub00000001"
    evidence = _make_evidence(overall="SCENARIO_FAILURE", exit_code=4)

    json_path, markdown_path = publish_evidence(evidence_dir, evidence, SecretRegistry())

    doc = json.loads(json_path.read_text())
    assert doc["overall"] == "SCENARIO_FAILURE"
    assert doc["exit_code"] == 4
    assert "rpub00000001" in markdown_path.read_text()
