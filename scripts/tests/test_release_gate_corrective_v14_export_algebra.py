"""Task 15 Task 11 corrective v14 (design spec Sections 7, 9): `_thaw()`
is now a genuinely SYMMETRIC fail-closed counterpart to `deep_freeze()`.

Before this corrective, `_thaw()`'s `types.MappingProxyType` branch
iterated `value.items()` and used each `key` completely unexamined --
`deep_freeze()`'s own `_validate_canonical_mapping_key()` sealing only
protects values that were themselves BUILT by `deep_freeze()`; `_thaw()`
must not assume every value reaching it necessarily came from there (its
own docstring already says so for LEAF values -- this closes the same
gap for mapping KEYS). A crafted `MappingProxyType({EvilStr("x"): 1})`
handed directly to `_thaw()` exported the `EvilStr` key completely
unchanged.
"""

import uuid
from enum import Enum
from pathlib import Path
from types import MappingProxyType

import pytest

from scripts.release_gate.evidence import (
    ReleaseEvidence,
    UnsupportedEvidenceValueError,
    _thaw,
    deep_freeze,
)


class _EvilStr(str):
    pass


class _MutableHashableKey:
    def __init__(self, value):
        self.value = value

    def __hash__(self):
        return hash(self.value)

    def __eq__(self, other):
        return isinstance(other, _MutableHashableKey) and other.value == self.value


class _ExampleEnum(Enum):
    VALUE = "value"


# --- Section 7.2: _thaw() rejects noncanonical mapping keys -----------------


def test_thaw_rejects_str_subclass_mapping_key():
    value = MappingProxyType({_EvilStr("x"): 1})
    with pytest.raises(UnsupportedEvidenceValueError):
        _thaw(value)


def test_thaw_rejects_custom_mapping_key():
    value = MappingProxyType({_MutableHashableKey("x"): 1})
    with pytest.raises(UnsupportedEvidenceValueError):
        _thaw(value)


def test_thaw_rejects_str_subclass_key_nested_inside_a_valid_mapping():
    value = MappingProxyType({"outer": MappingProxyType({_EvilStr("inner"): 1})})
    with pytest.raises(UnsupportedEvidenceValueError):
        _thaw(value)


# --- Section 7.1/7.3: _thaw() rejects every noncanonical value -------------


@pytest.mark.parametrize(
    "bad",
    [b"x", bytearray(b"x"), Path("/tmp/x"), uuid.uuid4(), _ExampleEnum.VALUE, object()],
)
def test_thaw_rejects_noncanonical_value(bad):
    with pytest.raises(UnsupportedEvidenceValueError):
        _thaw(bad)


@pytest.mark.parametrize(
    "bad",
    [b"x", bytearray(b"x"), Path("/tmp/x"), uuid.uuid4(), _ExampleEnum.VALUE, object()],
)
def test_thaw_rejects_noncanonical_value_nested_inside_a_valid_mapping(bad):
    value = MappingProxyType({"key": bad})
    with pytest.raises(UnsupportedEvidenceValueError):
        _thaw(value)


def test_thaw_rejects_noncanonical_value_nested_inside_a_tuple():
    with pytest.raises(UnsupportedEvidenceValueError):
        _thaw((1, 2, object()))


# --- Section 7.1: _thaw() still accepts the exact canonical algebra --------


def test_thaw_accepts_exact_canonical_frozen_tree():
    frozen = deep_freeze({"a": [1, 2], "b": {"flag": True}, "c": None})
    exported = _thaw(frozen)
    assert exported == {"a": [1, 2], "b": {"flag": True}, "c": None}
    assert type(exported) is dict
    assert type(exported["a"]) is list


def test_thaw_of_frozenset_produces_a_fresh_list():
    frozen = deep_freeze({1, 2, 3})
    exported = _thaw(frozen)
    assert type(exported) is list
    assert sorted(exported) == [1, 2, 3]


# --- Section 7.4: to_dict() stays fresh and detached ------------------------


def _valid_release_evidence_kwargs() -> dict:
    return {
        "schema_version": 1,
        "run": {
            "run_id": "rv14exportalgebra1",
            "git_sha": "a" * 40,
            "started_at": "2026-09-10T00:00:00+00:00",
            "finished_at": "2026-09-10T00:05:00+00:00",
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
        "scenarios": {"happy_path": {"name": "happy_path", "status": "PASS", "assertions": {"flag": True}}},
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


def test_to_dict_mutation_never_reaches_the_frozen_snapshot():
    evidence = ReleaseEvidence(**_valid_release_evidence_kwargs())
    doc = evidence.to_dict()
    doc["scenarios"]["happy_path"]["assertions"]["flag"] = False
    doc["run"]["authority"]["platform"] = "tampered"
    assert evidence.scenarios["happy_path"]["assertions"]["flag"] is True
    assert evidence.run["authority"]["platform"] == "linux"


def test_to_dict_returns_a_fresh_object_on_every_call():
    evidence = ReleaseEvidence(**_valid_release_evidence_kwargs())
    first = evidence.to_dict()
    second = evidence.to_dict()
    assert first is not second
    assert first["scenarios"] is not second["scenarios"]
