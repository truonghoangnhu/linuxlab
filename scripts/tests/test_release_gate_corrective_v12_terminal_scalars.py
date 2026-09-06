"""Task 15 Task 11 corrective v12 (design spec Sections 4-8): constructor
sealing for every top-level terminal scalar field on `ReleaseEvidence`
and `FailureRecord`.

Before this corrective, `ReleaseEvidence.schema_version` / `cleanup_status`
/ `overall` / `exit_code` were plain dataclass fields with no runtime type
check at all -- only fields already shaped as a dict/Mapping (`run`,
`environment`, `scenarios`, ...) were protected, because only those go
through `deep_freeze()` in `__post_init__`. A `str`/`int` SUBCLASS (or an
outright unsupported value) assigned directly to one of these four
top-level scalars sailed straight through construction unexamined and
could carry hidden mutable state or overridden `__eq__`/`__str__`
behavior while still comparing/printing as the plain builtin it merely
resembles -- exactly the same class of attack corrective v11 already
closed for every NESTED scalar leaf, just one level higher up the
object.

These are the mandatory RED tests from the v12 design spec, Section 28.
"""

from pathlib import Path

import pytest

from scripts.release_gate.evidence import (
    SCHEMA_VERSION,
    FailureRecord,
    ReleaseEvidence,
    UnsupportedEvidenceValueError,
)


class _EvilStr(str):
    """A `str` subclass -- satisfies `isinstance(value, str)` but is not
    an exact canonical `str`."""


class _EvilInt(int):
    """An `int` subclass -- same adversarial shape as `_EvilStr`, for
    numeric fields."""


class _EvilFloat(float):
    pass


def valid_release_evidence_kwargs() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": "rterminalscalar1",
            "git_sha": "a" * 40,
            "started_at": "2026-09-07T00:00:00+00:00",
            "finished_at": "2026-09-07T00:05:00+00:00",
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


def _make_evidence(**overrides) -> ReleaseEvidence:
    kwargs = valid_release_evidence_kwargs()
    kwargs.update(overrides)
    return ReleaseEvidence(**kwargs)


# --- valid construction still works ------------------------------------


def test_release_evidence_accepts_exact_builtin_top_level_scalars():
    evidence = _make_evidence()
    assert evidence.schema_version == SCHEMA_VERSION
    assert evidence.cleanup_status == "clean"
    assert evidence.overall == "PASS"
    assert evidence.exit_code == 0


# --- cleanup_status ------------------------------------------------------


def test_release_evidence_rejects_str_subclass_cleanup_status():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(cleanup_status=_EvilStr("clean"))


def test_release_evidence_rejects_bytes_cleanup_status():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(cleanup_status=b"clean")


def test_release_evidence_rejects_path_cleanup_status():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(cleanup_status=Path("/tmp/x"))


def test_release_evidence_rejects_custom_object_cleanup_status():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(cleanup_status=object())


# --- schema_version --------------------------------------------------------


def test_release_evidence_rejects_int_subclass_schema_version():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(schema_version=_EvilInt(SCHEMA_VERSION))


def test_release_evidence_rejects_bool_schema_version():
    # bool is an int subclass in Python -- `True == 1` -- but must never
    # be accepted where an exact int is required.
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(schema_version=True)


def test_release_evidence_rejects_str_schema_version():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(schema_version=str(SCHEMA_VERSION))


# --- overall -----------------------------------------------------------------


def test_release_evidence_rejects_str_subclass_overall():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(overall=_EvilStr("PASS"))


def test_release_evidence_rejects_bytes_overall():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(overall=b"PASS")


# --- exit_code -----------------------------------------------------------------


def test_release_evidence_rejects_int_subclass_exit_code():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(exit_code=_EvilInt(0))


def test_release_evidence_rejects_bool_exit_code():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(exit_code=False)


def test_release_evidence_rejects_float_exit_code():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(exit_code=_EvilFloat(0.0))


def test_release_evidence_rejects_str_exit_code():
    with pytest.raises(UnsupportedEvidenceValueError):
        _make_evidence(exit_code="0")


# --- FailureRecord ---------------------------------------------------------


def _valid_failure_record_kwargs() -> dict:
    return {
        "category": "SCENARIO_FAILURE",
        "stage": "happy_path",
        "code": "AssertionError",
        "safe_message": "an internal failure occurred",
    }


def test_failure_record_accepts_exact_builtin_fields():
    record = FailureRecord(**_valid_failure_record_kwargs())
    assert record.category == "SCENARIO_FAILURE"


@pytest.mark.parametrize("field_name", ["category", "stage", "code", "safe_message"])
def test_failure_record_rejects_str_subclass_in_every_field(field_name):
    kwargs = _valid_failure_record_kwargs()
    kwargs[field_name] = _EvilStr(kwargs[field_name])
    with pytest.raises(UnsupportedEvidenceValueError):
        FailureRecord(**kwargs)


@pytest.mark.parametrize("field_name", ["category", "stage", "code", "safe_message"])
def test_failure_record_rejects_bytes_in_every_field(field_name):
    kwargs = _valid_failure_record_kwargs()
    kwargs[field_name] = b"evil"
    with pytest.raises(UnsupportedEvidenceValueError):
        FailureRecord(**kwargs)


def test_failure_record_rejects_message_str_subclass():
    """Explicit spec Section 28 callout: failure message str subclass."""
    kwargs = _valid_failure_record_kwargs()
    kwargs["safe_message"] = _EvilStr("an internal failure occurred")
    with pytest.raises(UnsupportedEvidenceValueError):
        FailureRecord(**kwargs)
