"""Task 15 Task 11 corrective v13 (design spec Sections 7-16, Part C
33): every frozen terminal dataclass reachable from `ReleaseEvidence`/
`GateRunResult` is constructor-sealed -- no scalar subclass, raw UUID
object, Enum, bytes, Path, or opaque custom object may enter terminal
truth by identity, and no supporting dataclass may depend on a LATER
snapshot (evidence assembly, export) to reject a malformed value.

Before this corrective, `ScenarioResult` and `PassDecision` had no
`__post_init__` validation at all -- any value, of any type, could be
assigned to any of their fields. `OutboxInboxProof` already had a
`__post_init__`, but it used `isinstance()` (accepting an `int`/`bool`
subclass) rather than exact-type matching for `inbox_count`, and
coerced `event_uuid` through `str(self.event_uuid)` before validating
it -- silently accepting (and retaining, by identity) a raw
`uuid.UUID` object rather than requiring the canonical string the spec
requires.

Reachable frozen terminal dataclasses covered here (spec Section 7):
`ScenarioResult`, `PassDecision`, `OutboxInboxProof`. `FailureRecord`
is already sealed as of corrective v12 (see
test_release_gate_corrective_v12_terminal_scalars.py) and is not
duplicated here.
"""

import uuid
from pathlib import Path

import pytest

from scripts.release_gate.evidence import (
    InvalidProofError,
    OutboxInboxProof,
    PassDecision,
    ScenarioResult,
    UnsupportedEvidenceValueError,
)


class _EvilStr(str):
    pass


class _EvilInt(int):
    pass


class _EvilFloat(float):
    pass


class _EvilBool:
    """Not a real bool subclass (bool cannot be subclassed) -- an
    opaque object standing in for one, to prove `isinstance`-only
    boolean checks are not enough on their own."""


# --- ScenarioResult (spec Section 12) ----------------------------------------


def _valid_scenario_kwargs() -> dict:
    return {
        "name": "happy_path",
        "status": "PASS",
        "assertions": {"package_installed_and_activated": True},
        "duration_ms": 12.5,
        "detail": "",
    }


def test_scenario_result_accepts_valid_fields():
    result = ScenarioResult(**_valid_scenario_kwargs())
    assert result.name == "happy_path"
    assert result.status == "PASS"


@pytest.mark.parametrize("field_name,bad", [
    ("name", _EvilStr("happy_path")),
    ("status", _EvilStr("PASS")),
    ("detail", _EvilStr("")),
])
def test_scenario_result_rejects_str_subclass_fields(field_name, bad):
    kwargs = _valid_scenario_kwargs()
    kwargs[field_name] = bad
    with pytest.raises(UnsupportedEvidenceValueError):
        ScenarioResult(**kwargs)


@pytest.mark.parametrize("field_name,bad", [
    ("name", b"happy_path"),
    ("status", b"PASS"),
    ("detail", b""),
])
def test_scenario_result_rejects_bytes_fields(field_name, bad):
    kwargs = _valid_scenario_kwargs()
    kwargs[field_name] = bad
    with pytest.raises(UnsupportedEvidenceValueError):
        ScenarioResult(**kwargs)


def test_scenario_result_rejects_float_subclass_duration_ms():
    kwargs = _valid_scenario_kwargs()
    kwargs["duration_ms"] = _EvilFloat(12.5)
    with pytest.raises(UnsupportedEvidenceValueError):
        ScenarioResult(**kwargs)


def test_scenario_result_rejects_int_duration_ms():
    """duration_ms is schema-defined as an exact float (Section 8:
    "float -> exact schema-defined numeric type") -- every real call
    site (gate_runner._run_one_scenario()'s own `time.monotonic()`
    computation) already produces a genuine float, never a plain int."""
    kwargs = _valid_scenario_kwargs()
    kwargs["duration_ms"] = 12
    with pytest.raises(UnsupportedEvidenceValueError):
        ScenarioResult(**kwargs)


def test_scenario_result_rejects_bool_duration_ms():
    kwargs = _valid_scenario_kwargs()
    kwargs["duration_ms"] = True
    with pytest.raises(UnsupportedEvidenceValueError):
        ScenarioResult(**kwargs)


def test_scenario_result_rejects_path_detail():
    kwargs = _valid_scenario_kwargs()
    kwargs["detail"] = Path("/tmp/x")
    with pytest.raises(UnsupportedEvidenceValueError):
        ScenarioResult(**kwargs)


def test_scenario_result_rejects_opaque_object_assertions():
    kwargs = _valid_scenario_kwargs()
    kwargs["assertions"] = object()
    with pytest.raises(UnsupportedEvidenceValueError):
        ScenarioResult(**kwargs)


def test_scenario_result_rejects_uuid_in_assertions():
    kwargs = _valid_scenario_kwargs()
    kwargs["assertions"] = {"some_key": uuid.uuid4()}
    with pytest.raises(UnsupportedEvidenceValueError):
        ScenarioResult(**kwargs)


def test_scenario_result_assertions_are_detached_and_frozen():
    """Matches ReleaseEvidence's own deep_freeze() detachment guarantee
    (corrective v9) -- mutating the caller's original dict after
    construction must never be reflected in the sealed record."""
    original = {"package_installed_and_activated": True}
    result = ScenarioResult(name="happy_path", status="PASS", assertions=original)
    original["package_installed_and_activated"] = False
    assert result.assertions["package_installed_and_activated"] is True
    with pytest.raises(TypeError):
        result.assertions["package_installed_and_activated"] = False


# --- PassDecision (spec Section 11) ------------------------------------------


def _valid_pass_decision_kwargs() -> dict:
    return {"eligible": True, "reasons": ()}


def test_pass_decision_accepts_valid_fields():
    decision = PassDecision(**_valid_pass_decision_kwargs())
    assert decision.eligible is True
    assert decision.reasons == ()


def test_pass_decision_accepts_ineligible_with_reasons():
    decision = PassDecision(eligible=False, reasons=("run_id_missing", "clean_tree_not_true"))
    assert decision.eligible is False
    assert decision.reasons == ("run_id_missing", "clean_tree_not_true")


def test_pass_decision_rejects_non_bool_eligible():
    with pytest.raises(UnsupportedEvidenceValueError):
        PassDecision(eligible=1, reasons=())


def test_pass_decision_rejects_opaque_object_eligible():
    with pytest.raises(UnsupportedEvidenceValueError):
        PassDecision(eligible=_EvilBool(), reasons=())


def test_pass_decision_rejects_list_reasons():
    with pytest.raises(UnsupportedEvidenceValueError):
        PassDecision(eligible=False, reasons=["run_id_missing"])


def test_pass_decision_rejects_non_str_reason_entry():
    with pytest.raises(UnsupportedEvidenceValueError):
        PassDecision(eligible=False, reasons=(b"run_id_missing",))


def test_pass_decision_rejects_str_subclass_reason_entry():
    with pytest.raises(UnsupportedEvidenceValueError):
        PassDecision(eligible=False, reasons=(_EvilStr("run_id_missing"),))


def test_pass_decision_rejects_eligible_true_with_nonempty_reasons():
    """Section 11: PassDecision's own invariant -- reasons is empty iff
    eligible is True."""
    with pytest.raises(UnsupportedEvidenceValueError):
        PassDecision(eligible=True, reasons=("run_id_missing",))


# --- OutboxInboxProof (spec Section 13) --------------------------------------


def _valid_outbox_kwargs() -> dict:
    return {
        "event_uuid": str(uuid.uuid4()),
        "inbox_count": 1,
        "node_state": "DELIVERED",
        "duplicate_idempotent": True,
    }


def test_outbox_inbox_proof_accepts_valid_fields():
    kwargs = _valid_outbox_kwargs()
    proof = OutboxInboxProof(**kwargs)
    assert proof.event_uuid == kwargs["event_uuid"]


@pytest.mark.parametrize("bad", [uuid.uuid4(), b"raw", Path("/tmp/x"), object(), 12345])
def test_outbox_inbox_proof_rejects_noncanonical_event_uuid(bad):
    """Spec Section 9/13: a raw UUID object (or any other non-str
    stand-in) must never be retained by identity -- only an exact
    canonical str, already validated as UUID-parseable, is accepted."""
    kwargs = _valid_outbox_kwargs()
    kwargs["event_uuid"] = bad
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**kwargs)


def test_outbox_inbox_proof_rejects_str_subclass_event_uuid():
    kwargs = _valid_outbox_kwargs()
    kwargs["event_uuid"] = _EvilStr(kwargs["event_uuid"])
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**kwargs)


def test_outbox_inbox_proof_rejects_malformed_event_uuid_string():
    kwargs = _valid_outbox_kwargs()
    kwargs["event_uuid"] = "not-a-uuid"
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**kwargs)


def test_outbox_inbox_proof_rejects_int_subclass_inbox_count():
    kwargs = _valid_outbox_kwargs()
    kwargs["inbox_count"] = _EvilInt(1)
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**kwargs)


def test_outbox_inbox_proof_rejects_bool_inbox_count():
    kwargs = _valid_outbox_kwargs()
    kwargs["inbox_count"] = True
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**kwargs)


def test_outbox_inbox_proof_rejects_negative_inbox_count():
    kwargs = _valid_outbox_kwargs()
    kwargs["inbox_count"] = -1
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**kwargs)


def test_outbox_inbox_proof_rejects_str_subclass_node_state():
    kwargs = _valid_outbox_kwargs()
    kwargs["node_state"] = _EvilStr("DELIVERED")
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**kwargs)


def test_outbox_inbox_proof_rejects_unknown_node_state():
    kwargs = _valid_outbox_kwargs()
    kwargs["node_state"] = "UNKNOWN"
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**kwargs)


def test_outbox_inbox_proof_rejects_non_bool_duplicate_idempotent():
    kwargs = _valid_outbox_kwargs()
    kwargs["duplicate_idempotent"] = 1
    with pytest.raises(InvalidProofError):
        OutboxInboxProof(**kwargs)
