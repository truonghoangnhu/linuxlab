"""Task 15 Task 11 corrective v11 (design spec Sections 4-12): the
canonical terminal value algebra. `deep_freeze()` previously accepted
any mapping KEY unchanged (no type check at all) and accepted any
scalar via permissive `isinstance()` -- a `str` subclass with mutable
attributes or overridden behavior satisfied `isinstance(value, str)`
and was retained as-is, and a custom/mutable hashable object used as a
dict key was retained as-is too. Both are closed here: every terminal
mapping key must be an exact canonical `str`, and every terminal leaf
scalar must be an EXACT builtin type (`type(value) is str`, never
`isinstance`), never a subclass.

These are the mandatory RED tests from the v11 design spec, Section 32,
items 1-5.
"""

import types
from collections.abc import Mapping

import pytest

from scripts.release_gate.evidence import (
    SCHEMA_VERSION,
    ReleaseEvidence,
    UnsupportedEvidenceValueError,
    deep_freeze,
)


def _make_evidence(**overrides):
    fields = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": "rcanonicaltest1",
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


class _MutableHashableKey:
    """An adversarial mapping key: mutable state feeds its own
    __hash__/__eq__, so its identity as a dict key can change after the
    dict/mapping has already been handed to deep_freeze()."""

    def __init__(self, label):
        self.label = label

    def __hash__(self):
        return hash(self.label)

    def __eq__(self, other):
        return isinstance(other, _MutableHashableKey) and self.label == other.label

    def __str__(self):
        return self.label


class _StrSubclassKey(str):
    """A `str` subclass key -- satisfies `isinstance(key, str)` but is
    not an exact canonical `str`."""


class _MutableStringMapping(Mapping):
    """A custom collections.abc.Mapping implementation (never a plain
    dict or MappingProxyType) backed by a mutable dict the caller keeps
    a live reference to."""

    def __init__(self, backing):
        self._backing = backing

    def __getitem__(self, key):
        return self._backing[key]

    def __iter__(self):
        return iter(self._backing)

    def __len__(self):
        return len(self._backing)


# --- RED test 32.1: mutable/custom mapping key rejection --------------------


def test_deep_freeze_rejects_mutable_hashable_key_inside_plain_dict():
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze({_MutableHashableKey("a"): True})


def test_deep_freeze_rejects_str_subclass_key_inside_plain_dict():
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze({_StrSubclassKey("central_check"): "ok"})


def test_deep_freeze_rejects_mutable_key_inside_mapping_proxy_type():
    backing = {_MutableHashableKey("central_check"): False}
    proxy = types.MappingProxyType(backing)
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze(proxy)


def test_deep_freeze_rejects_mutable_key_inside_custom_abc_mapping():
    custom_mapping = _MutableStringMapping({_MutableHashableKey("clean_tree"): True})
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze(custom_mapping)


def test_snapshot_construction_rejects_mutable_mapping_key_in_validation_field():
    validation = {
        "clean_tree": True,
        "central_check": "ok",
        "node_check": "ok",
        "central_migration_drift": "none",
        "node_migration_drift": "none",
        _MutableHashableKey("extra"): "value",
    }
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(validation=validation)


# --- RED test 32.2: published authority key attack must fail ----------------


def test_mapping_key_alias_attack_cannot_alter_returned_or_persisted_authority():
    """The v10 alias attack mutated a VALUE reachable through a live
    MappingProxyType-over-mutable-dict alias. This is the v11 analogue
    for KEYS: a caller-owned mutable/custom key object backing a proxy
    must never be admitted at all, so there is no possible path where
    mutating it after publication changes returned authority or
    persisted evidence -- construction itself must fail closed."""
    backing = {_MutableHashableKey("authoritative"): False}
    proxy = types.MappingProxyType(backing)
    run = {
        "run_id": "rcanonicaltest2",
        "git_sha": "a" * 40,
        "started_at": "2026-09-06T00:00:00+00:00",
        "finished_at": "2026-09-06T00:05:00+00:00",
        "authority": proxy,
    }
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(run=run)


# --- RED test 32.3: scalar subclass rejection --------------------------------


class _MutableString(str):
    """A `str` subclass -- satisfies isinstance(value, str) but is not
    an exact canonical str, and could carry additional mutable
    attributes/overridden behavior a plain str cannot."""


class _MutableInt(int):
    """An `int` subclass -- same adversarial shape as _MutableString,
    for numeric fields."""


def test_deep_freeze_rejects_str_subclass_scalar_value():
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze(_MutableString("failure"))


def test_deep_freeze_rejects_int_subclass_scalar_value():
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze(_MutableInt(1))


def test_snapshot_construction_rejects_str_subclass_in_nested_scenario_field():
    scenarios = {
        "happy_path": {
            "name": "happy_path",
            "status": _MutableString("PASS"),
            "assertions": {},
            "duration_ms": 1.0,
            "detail": "",
        }
    }
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(scenarios=scenarios)


def test_snapshot_construction_rejects_str_subclass_in_proof_field():
    proofs = {"package_sha256": _MutableString("a" * 64)}
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(proofs=proofs)


def test_snapshot_construction_rejects_str_subclass_in_primary_failure_field():
    primary_failure = {
        "category": "SCENARIO_FAILURE",
        "stage": "happy_path",
        "code": "AssertionError",
        "safe_message": _MutableString("an internal failure occurred"),
    }
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(primary_failure=primary_failure, overall="SCENARIO_FAILURE", exit_code=4)


def test_deep_freeze_still_accepts_exact_builtin_scalars_unchanged():
    assert deep_freeze("x") == "x"
    assert type(deep_freeze("x")) is str
    assert deep_freeze(1) == 1
    assert type(deep_freeze(1)) is int
    assert deep_freeze(1.5) == 1.5
    assert deep_freeze(True) is True
    assert deep_freeze(None) is None
    # Task 15 Task 11 corrective v12, Section 4/10: `bytes` was removed
    # from the canonical terminal scalar algebra -- it is no longer a
    # safe leaf, and now raises like any other unsupported value (see
    # test_release_gate_corrective_v12_export_algebra.py).
    with pytest.raises(UnsupportedEvidenceValueError):
        deep_freeze(b"raw-bytes")


# --- RED test 32.4: renderer exact-leaf export -------------------------------


def test_to_dict_output_contains_only_exact_builtin_leaves_and_mutating_it_is_inert():
    evidence = _make_evidence()
    doc = evidence.to_dict()

    def _assert_exact_builtins(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert type(key) is str
                _assert_exact_builtins(item)
        elif isinstance(value, list):
            for item in value:
                _assert_exact_builtins(item)
        else:
            assert type(value) in (str, int, float, bool, type(None))

    _assert_exact_builtins(doc)

    doc["validation"]["clean_tree"] = False
    assert evidence.validation["clean_tree"] is True


# --- RED test 32.5: emergency graph canonicality -----------------------------


def test_gate_runner_emergency_results_graph_is_canonical_and_immune_to_poisoning():
    from scripts.release_gate import gate_runner

    def _assert_exact_builtins(value):
        if isinstance(value, (dict, types.MappingProxyType, Mapping)):
            for key, item in value.items():
                assert type(key) is str
                _assert_exact_builtins(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _assert_exact_builtins(item)
        else:
            assert type(value) in (str, int, float, bool, type(None))

    for result in gate_runner._EMERGENCY_RESULTS.values():
        assert result.evidence is None
        _assert_exact_builtins(result.terminal_failure)

        with pytest.raises(TypeError):
            result.terminal_failure["safe_message"] = "poisoned"
