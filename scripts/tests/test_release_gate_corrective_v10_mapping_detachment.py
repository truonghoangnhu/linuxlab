"""Task 15 Task 11 corrective v10, Gap 2 (design spec Sections 7-11):
`deep_freeze()` only ever recursed into a plain `dict`
(`isinstance(value, dict)`) -- a `types.MappingProxyType` is NOT a
`dict` subclass, so a value that was ALREADY a `MappingProxyType`
wrapping a plain mutable backing dict fell through to `return value`
UNCHANGED. The same live, read-only VIEW over someone else's mutable
backing dict then ended up stored directly in supposedly-immutable
evidence -- mutating that external backing dict after construction (or
even after publication) showed straight through, exactly as if
`ReleaseEvidence` were not immutable at all.

These are the mandatory RED tests from the v10 design spec, Section 25,
items 4-6, plus the Section 9 "unsupported mutable custom value" case.
"""

import json
import types

import pytest

from scripts.release_gate.evidence import (
    SCHEMA_VERSION,
    ReleaseEvidence,
    SecretRegistry,
    UnsupportedEvidenceValueError,
    deep_freeze,
    publish_evidence,
)


def _make_evidence(**overrides):
    fields = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": "rmappingtest1",
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


# --- RED test 25.4: MappingProxyType backing alias --------------------------


def test_mapping_proxy_authority_backing_alias_does_not_affect_snapshot():
    backing_authority = {"authoritative": True, "platform": "linux", "reason": None}
    proxy_authority = types.MappingProxyType(backing_authority)
    run = {
        "run_id": "rproxytest001",
        "git_sha": "a" * 40,
        "started_at": "2026-09-06T00:00:00+00:00",
        "finished_at": "2026-09-06T00:05:00+00:00",
        "authority": proxy_authority,
    }
    evidence = _make_evidence(run=run)

    # Mutate the ORIGINAL backing dict AFTER construction.
    backing_authority["authoritative"] = False

    assert evidence.run["authority"]["authoritative"] is True


def test_mapping_proxy_authority_backing_alias_does_not_affect_published_evidence(tmp_path):
    """Spec Section 11's own permanent adversarial case: mutating the
    backing dict AFTER publication must never change persisted JSON,
    persisted Markdown, or the returned snapshot."""
    backing_authority = {"authoritative": False}
    proxy_authority = types.MappingProxyType(backing_authority)
    run = {
        "run_id": "rproxytest002",
        "git_sha": "a" * 40,
        "started_at": "2026-09-06T00:00:00+00:00",
        "finished_at": "2026-09-06T00:05:00+00:00",
        "authority": proxy_authority,
    }
    evidence = _make_evidence(run=run, overall="CLEANUP_FAILURE", exit_code=5)

    evidence_dir = tmp_path / "release-evidence"
    secrets = SecretRegistry()
    json_path, markdown_path = publish_evidence(evidence_dir, evidence, secrets)

    # Mutate the backing dict AFTER publication.
    backing_authority["authoritative"] = True

    published_doc = json.loads(json_path.read_text())
    assert published_doc["run"]["authority"]["authoritative"] is False
    assert "authoritative: True" not in markdown_path.read_text()
    assert evidence.run["authority"]["authoritative"] is False


# --- RED test 25.5: nested proxy alias (proxy contains nested mutable ------
# --- dict/list values) ------------------------------------------------------


def test_nested_mapping_proxy_backing_alias_with_nested_mutable_containers():
    inner_mutable_list = [1, 2, 3]
    inner_mutable_dict = {"flag": True}
    backing = {"items": inner_mutable_list, "sub": inner_mutable_dict}
    proxy = types.MappingProxyType(backing)

    validation = {
        "clean_tree": True,
        "central_check": "ok",
        "node_check": "ok",
        "central_migration_drift": "none",
        "node_migration_drift": "none",
        "extra_proxy_field": proxy,
    }
    evidence = _make_evidence(validation=validation)

    # Mutate the nested mutable containers AFTER construction.
    inner_mutable_list.append(4)
    inner_mutable_dict["flag"] = False
    backing["items"] = "replaced"

    frozen_extra = evidence.validation["extra_proxy_field"]
    assert list(frozen_extra["items"]) == [1, 2, 3]
    assert frozen_extra["sub"]["flag"] is True


# --- RED test 25.6: renderer export mutation (re-confirmed for the --------
# --- Gap 2 code path specifically) ------------------------------------------


def test_to_dict_output_from_proxy_backed_evidence_is_a_fresh_mutable_copy():
    backing_authority = {"authoritative": True, "platform": "linux", "reason": None}
    proxy_authority = types.MappingProxyType(backing_authority)
    run = {
        "run_id": "rproxytest003",
        "git_sha": "a" * 40,
        "started_at": "2026-09-06T00:00:00+00:00",
        "finished_at": "2026-09-06T00:05:00+00:00",
        "authority": proxy_authority,
    }
    evidence = _make_evidence(run=run)

    doc = evidence.to_dict()
    doc["run"]["authority"]["authoritative"] = False
    doc["run"]["run_id"] = "MUTATED"

    assert evidence.run["authority"]["authoritative"] is True
    assert evidence.run["run_id"] == "rproxytest003"
    # The rendered doc must be genuinely JSON-serializable plain data,
    # not a leftover MappingProxyType.
    json.dumps(doc)
    assert type(doc["run"]["authority"]) is dict


# --- Section 9: unsupported mutable custom values are rejected -------------


class _MutableCustomThing:
    """An intentionally-crafted adversarial input: a custom class
    instance with a mutable attribute that is neither a Mapping, list/
    tuple, set/frozenset, nor a known-safe scalar."""

    def __init__(self):
        self.value = 1


def test_deep_freeze_rejects_unsupported_mutable_custom_value_directly():
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze(_MutableCustomThing())


def test_snapshot_construction_rejects_unsupported_mutable_custom_value():
    validation = {
        "clean_tree": True,
        "central_check": "ok",
        "node_check": "ok",
        "central_migration_drift": "none",
        "node_migration_drift": "none",
        "suspect": _MutableCustomThing(),
    }
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(validation=validation)


def test_deep_freeze_still_accepts_every_known_safe_scalar_and_container_type():
    assert deep_freeze("x") == "x"
    assert deep_freeze(1) == 1
    assert deep_freeze(1.5) == 1.5
    assert deep_freeze(True) is True
    assert deep_freeze(None) is None
    assert deep_freeze(b"raw-bytes") == b"raw-bytes"

    frozen = deep_freeze({"a": [1, 2], "b": (3, 4), "c": {5, 6}})
    assert isinstance(frozen, types.MappingProxyType)
    assert frozen["a"] == (1, 2)
    assert frozen["b"] == (3, 4)
    assert frozen["c"] == frozenset({5, 6})

    # Idempotent re-freezing of an already-frozen MappingProxyType.
    refrozen = deep_freeze(frozen)
    assert isinstance(refrozen, types.MappingProxyType)
    assert refrozen == frozen
