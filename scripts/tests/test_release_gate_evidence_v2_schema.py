"""Task 15 Task 11 corrective v2, Section 9 ("Canonical EvidenceV1 v1"):
the schema gains `environment`, `profiles`, `run.authority`, `retention`
(replacing the old `retained_resources` -- execution resources are now
NEVER retained, see Section 3), and `finalization_failure`.
"""

from scripts.release_gate.environment import Authority
from scripts.release_gate.evidence import SCHEMA_VERSION, ReleaseEvidence


def _make_v2_evidence(**overrides):
    fields = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": "rabc123456789",
            "git_sha": "a" * 40,
            "started_at": "2026-09-04T00:00:00+00:00",
            "finished_at": "2026-09-04T00:05:00+00:00",
            "authority": Authority(authoritative=True, platform="linux", reason=None).__dict__,
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


def test_evidence_v2_to_dict_has_the_full_v2_schema_shape():
    evidence = _make_v2_evidence()
    doc = evidence.to_dict()

    assert doc["schema_version"] == 1
    assert doc["run"]["authority"] == {"authoritative": True, "platform": "linux", "reason": None}
    assert doc["environment"] == {
        "platform": "linux",
        "python_version": "3.12.0",
        "django_version": "5.2",
        "postgres_version": "180000",
    }
    assert doc["profiles"] == {"central": "linuxlab.settings.central", "node": "linuxlab.settings.node"}
    assert doc["retention"] == {
        "keep_on_failure_requested": False,
        "execution_resources_retained": False,
        "sanitized_debug_bundle_retained": True,
    }
    assert "finalization_failure" in doc
    assert doc["finalization_failure"] is None
    assert "retained_resources" not in doc


def test_evidence_v2_retention_never_claims_execution_resources_retained():
    """Structural invariant (Section 3.1): execution_resources_retained
    must always be False -- there is no code path that legitimately
    sets it True, since execution resources are never retained."""
    evidence = _make_v2_evidence(
        retention={
            "keep_on_failure_requested": True,
            "execution_resources_retained": False,
            "sanitized_debug_bundle_retained": True,
        }
    )
    assert evidence.to_dict()["retention"]["execution_resources_retained"] is False


def test_evidence_v2_finalization_failure_carries_a_failure_record_dict():
    from scripts.release_gate.evidence import FailureRecord

    record = FailureRecord(
        category="CLEANUP_FAILURE", stage="finalize", code="SecretLeakError", safe_message="x"
    )
    evidence = _make_v2_evidence(finalization_failure=record.to_dict(), overall="CLEANUP_FAILURE", exit_code=5)
    assert evidence.to_dict()["finalization_failure"]["code"] == "SecretLeakError"
