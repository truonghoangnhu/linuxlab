"""Pure diagnostic contract tests; no deployment providers or database access."""

import json
from dataclasses import FrozenInstanceError

import pytest

from linuxlab.core import diagnostics as d


def test_warn_aggregation():
    from linuxlab.core.diagnostics import (
        DiagnosticCheck,
        DiagnosticContext,
        aggregate_diagnostics,
    )

    context = DiagnosticContext("node", "local", frozenset())
    report = aggregate_diagnostics(
        context,
        [
            DiagnosticCheck("SCHEMA_STATE", "WARN", "Review schema."),
            DiagnosticCheck("DB_CONNECTIVITY", "PASS", "Database available."),
        ],
    )
    assert report.overall_status == "WARN"
    assert tuple(c.code for c in report.checks) == ("DB_CONNECTIVITY", "SCHEMA_STATE")


def context():
    return d.DiagnosticContext("node", "local", frozenset())


def wire():
    return {
        "schema_version": 1,
        "profile": "node",
        "mode": "local",
        "overall_status": "PASS",
        "checks": [{"code": "DB_CONNECTIVITY", "status": "PASS", "message": "Ready"}],
    }


def validate(value):
    return d.validate_diagnostic_report(
        value, context=context(), required_codes=frozenset({"DB_CONNECTIVITY"})
    )


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([], "PASS"),
        (["PASS"], "PASS"),
        (["WARN", "FAIL", "PASS"], "FAIL"),
        (["FAIL", "WARN"], "FAIL"),
    ],
)
def test_aggregation_precedence(statuses, expected):
    codes = ("TRUST_KEY", "SCHEMA_STATE", "DB_CONNECTIVITY")
    report = d.aggregate_diagnostics(
        context(),
        [d.DiagnosticCheck(code, status, "arbitrary") for code, status in zip(codes, statuses)],
    )
    assert report.overall_status == expected
    assert [c.code for c in report.checks] == sorted(codes[: len(statuses)])


@pytest.mark.parametrize(
    "profile,mode",
    [("test", "local"), (None, "local"), ([], "local"), ("node", "automatic"), ("central", True)],
)
def test_invalid_context(profile, mode):
    with pytest.raises(d.DiagnosticContractError):
        d.DiagnosticContext(profile, mode, frozenset())


@pytest.mark.parametrize("capabilities", ["LINUXLAB_CAN_AUTHOR", [True], ["UNKNOWN"]])
def test_invalid_capabilities(capabilities):
    with pytest.raises(d.DiagnosticContractError):
        d.DiagnosticContext("node", "local", capabilities)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("schema_version", 1.0),
        ("profile", "central"),
        ("mode", "remote"),
        ("overall_status", "WARN"),
        ("overall_status", "SKIPPED"),
        ("checks", {}),
        ("checks", [None]),
    ],
)
def test_invalid_report_fields(field, value):
    payload = wire()
    payload[field] = value
    with pytest.raises(d.DiagnosticContractError):
        validate(payload)


@pytest.mark.parametrize("value", [None, [], 1, True, b"{}", "null", "[]", "{", '{"x":NaN}'])
def test_invalid_report_structure(value):
    with pytest.raises(d.DiagnosticContractError):
        validate(value)


@pytest.mark.parametrize("field", ["schema_version", "profile", "mode", "overall_status", "checks"])
def test_missing_report_fields(field):
    payload = wire()
    del payload[field]
    with pytest.raises(d.DiagnosticContractError):
        validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "UNKNOWN"),
        ("code", []),
        ("status", "SKIPPED"),
        ("status", True),
        ("message", None),
        ("message", "x" * 513),
        ("details", None),
        ("extra", "secret"),
    ],
)
def test_invalid_check_fields(field, value):
    payload = wire()
    payload["checks"][0][field] = value
    with pytest.raises(d.DiagnosticContractError):
        validate(payload)


def test_extra_report_field():
    payload = wire()
    payload["secret"] = "REJECTED_SENTINEL"
    with pytest.raises(d.DiagnosticContractError):
        validate(payload)


@pytest.mark.parametrize("field", ["code", "status", "message"])
def test_missing_check_fields(field):
    payload = wire()
    del payload["checks"][0][field]
    with pytest.raises(d.DiagnosticContractError):
        validate(payload)


def test_duplicate_and_required_codes():
    payload = wire()
    payload["checks"] *= 2
    with pytest.raises(d.DiagnosticContractError):
        validate(payload)
    payload["checks"] = []
    with pytest.raises(d.DiagnosticContractError):
        validate(payload)
    with pytest.raises(d.DiagnosticContractError):
        d.aggregate_diagnostics(context(), [d.DiagnosticCheck("DB_CONNECTIVITY", "PASS", "")] * 2)


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(wire()).replace('"schema_version": 1', '"schema_version":1,"schema_version":1'),
        json.dumps(wire()).replace('"status": "PASS"', '"status":"PASS","status":"PASS"'),
        json.dumps(wire()).replace(
            '"message": "Ready"',
            '"message":"Ready","details":{"error_code":"SAFE","error_code":"SAFE"}',
        ),
    ],
)
def test_duplicate_json_members(raw):
    with pytest.raises(d.DiagnosticContractError):
        validate(raw)


@pytest.mark.parametrize(
    "code,details",
    [
        ("DB_CONNECTIVITY", {"http_status": 200}),
        ("TRUST_KEY", {"node_count": 0}),
        ("NODE_REGISTRATION", {"node_count": True}),
        ("NODE_REGISTRATION", {"node_count": -1}),
        ("NODE_REGISTRATION", {"invalid_count": 1.5}),
        ("NODE_REGISTRATION", {"authority_verified": 1}),
        ("CENTRAL_CONNECTIVITY", {"http_status": True}),
        ("CENTRAL_CONNECTIVITY", {"http_status": 99}),
        ("CENTRAL_AUTH_ACCEPTANCE", {"http_status": 600}),
        ("CENTRAL_AUTH_ACCEPTANCE", {"http_status": 200.0}),
        ("TRUST_KEY", {"error_code": "lower"}),
        ("TRUST_KEY", {"error_code": ""}),
        ("TRUST_KEY", {"error_code": "A" * 101}),
        ("TRUST_KEY", {"error_code": "A\n"}),
        ("TRUST_KEY", {"error_code": {"secret": "REJECTED_SENTINEL"}}),
        ("TRUST_KEY", {"error_code": float("inf")}),
        ("TRUST_KEY", {"Authorization": "REJECTED_SENTINEL"}),
    ],
)
def test_detail_allowlist_rejections(code, details):
    with pytest.raises(d.DiagnosticContractError):
        d.DiagnosticCheck(code, "PASS", "", details)


@pytest.mark.parametrize(
    "code,details",
    [
        ("TRUST_KEY", {"error_code": "A" * 100}),
        ("NODE_REGISTRATION", {"node_count": 0, "invalid_count": 1, "authority_verified": False}),
        ("CENTRAL_CONNECTIVITY", {"http_status": 100}),
        ("CENTRAL_AUTH_ACCEPTANCE", {"http_status": 599}),
        ("CENTRAL_AUTH_ACCEPTANCE", {"http_status": None}),
    ],
)
def test_detail_allowlist_acceptance(code, details):
    report = d.aggregate_diagnostics(context(), [d.DiagnosticCheck(code, "PASS", "", details)])
    assert d.report_to_dict(report)["checks"][0]["details"] == details


def test_message_and_details_boundaries():
    d.DiagnosticCheck("TRUST_KEY", "PASS", "😀" * 512)
    # 15 bytes of JSON punctuation/key + 4081 ASCII digits = 4096 bytes.
    details = {"node_count": 10**4080}
    assert len(json.dumps(details, separators=(",", ":")).encode()) == 4096
    d.DiagnosticCheck("NODE_REGISTRATION", "PASS", "", details)
    with pytest.raises(d.DiagnosticContractError):
        d.DiagnosticCheck("NODE_REGISTRATION", "PASS", "", {"node_count": 10**4081})


def test_report_input_byte_boundary():
    payload = wire()
    payload["checks"][0]["message"] = "😀"
    raw = json.dumps(payload, ensure_ascii=False)
    exact = raw + " " * (65536 - len(raw.encode()))
    assert validate(exact).overall_status == "PASS"
    with pytest.raises(d.DiagnosticContractError):
        validate(exact + " ")


def test_immutable_inputs_and_exports():
    details = {"node_count": 1}
    check = d.DiagnosticCheck("NODE_REGISTRATION", "PASS", "", details)
    checks = [check]
    report = d.DiagnosticReport(1, "node", "local", "PASS", checks)
    caps = {"LINUXLAB_CAN_AUTHOR"}
    ctx = d.DiagnosticContext("central", "local", caps)
    details["node_count"] = 99
    checks.clear()
    caps.clear()
    exported = d.report_to_dict(report)
    exported["checks"][0]["details"]["node_count"] = 100
    assert report.checks[0].details["node_count"] == 1
    assert ctx.capabilities == frozenset({"LINUXLAB_CAN_AUTHOR"})
    with pytest.raises(TypeError):
        check.details["node_count"] = 3
    with pytest.raises(FrozenInstanceError):
        report.overall_status = "FAIL"


def test_roundtrip_order_and_message_independence():
    checks = [
        d.DiagnosticCheck("TRUST_KEY", "WARN", "FAIL"),
        d.DiagnosticCheck("DB_CONNECTIVITY", "PASS", "WARN"),
    ]
    report = d.aggregate_diagnostics(context(), checks)
    raw = d.report_to_json(report)
    restored = d.validate_diagnostic_report(raw, context=context(), required_codes=frozenset())
    assert restored == report
    assert d.report_to_dict(restored)["checks"][0]["code"] == "DB_CONNECTIVITY"
    assert d.report_to_json(restored) == raw
    assert restored.overall_status == "WARN"
    assert validate(wire()).checks[0].details == {}


@pytest.mark.parametrize(
    "construct",
    [
        lambda: d.DiagnosticCheck("REJECTED_SENTINEL", "PASS", ""),
        lambda: d.DiagnosticCheck("TRUST_KEY", "PASS", "", {"REJECTED_SENTINEL": "secret"}),
        lambda: validate('{"REJECTED_SENTINEL": invalid}'),
        lambda: d.DiagnosticReport(True, "node", "local", "PASS", ()),
        lambda: d.DiagnosticReport(1, "node", "local", "FAIL", ()),
    ],
)
def test_safe_contract_errors(construct):
    with pytest.raises(d.DiagnosticContractError) as caught:
        construct()
    assert "REJECTED_SENTINEL" not in str(caught.value)
    assert "REJECTED_SENTINEL" not in repr(caught.value)


@pytest.fixture
def isolated_registry(monkeypatch):
    # Restore any pre-existing registry after every test; no cleanup API in core.
    monkeypatch.setattr(d, "_factories", {}, raising=False)


class FakeProvider:
    codes = frozenset({"DB_CONNECTIVITY"})

    def required_codes(self, ctx):
        return self.codes if ctx.mode == "local" else frozenset()

    def checks(self, ctx):
        raise AssertionError("Contract operations must not execute checks")


class TrustProvider(FakeProvider):
    codes = frozenset({"TRUST_KEY"})


def test_registry_lazy_idempotent_fresh_and_required_codes(isolated_registry):
    constructed = []

    def factory():
        provider = FakeProvider()
        constructed.append(provider)
        return provider

    d.register_diagnostic_provider("core", factory)
    d.register_diagnostic_provider("core", factory)
    d.register_diagnostic_provider("trust", TrustProvider)
    assert constructed == []
    first = d.diagnostic_providers()
    second = d.diagnostic_providers()
    assert len(first) == len(second) == 2
    assert first[0] is not second[0]
    assert len(constructed) == 2
    assert d.required_check_codes(context(), first) == frozenset({"DB_CONNECTIVITY", "TRUST_KEY"})
    assert (
        d.required_check_codes(d.DiagnosticContext("node", "remote", frozenset()), first)
        == frozenset()
    )


def test_registry_conflicting_name(isolated_registry):
    d.register_diagnostic_provider("core", FakeProvider)
    with pytest.raises(d.DiagnosticContractError):
        d.register_diagnostic_provider("core", TrustProvider)
    assert d.diagnostic_providers()[0].codes == FakeProvider.codes


def test_registry_conflicting_codes(isolated_registry):
    d.register_diagnostic_provider("one", FakeProvider)
    d.register_diagnostic_provider("two", FakeProvider)
    with pytest.raises(d.DiagnosticContractError):
        d.diagnostic_providers()
    with pytest.raises(d.DiagnosticContractError):
        d.required_check_codes(context(), (FakeProvider(), FakeProvider()))


@pytest.mark.parametrize(
    "name,factory",
    [("", FakeProvider), (None, FakeProvider), ("core", None), ("core", FakeProvider())],
)
def test_invalid_registration(isolated_registry, name, factory):
    with pytest.raises(d.DiagnosticContractError):
        d.register_diagnostic_provider(name, factory)


@pytest.mark.parametrize("codes", [frozenset({"UNKNOWN"}), frozenset({True}), {"TRUST_KEY"}])
def test_invalid_provider_codes(isolated_registry, codes):
    class InvalidProvider(FakeProvider):
        pass

    InvalidProvider.codes = codes
    d.register_diagnostic_provider("invalid", InvalidProvider)
    with pytest.raises(d.DiagnosticContractError):
        d.diagnostic_providers()


@pytest.mark.parametrize(
    "required", [frozenset({"TRUST_KEY"}), {"DB_CONNECTIVITY"}, frozenset({"UNKNOWN"})]
)
def test_provider_cannot_require_unowned_or_invalid_codes(required):
    class InvalidProvider(FakeProvider):
        def required_codes(self, ctx):
            return required

    with pytest.raises(d.DiagnosticContractError):
        d.required_check_codes(context(), (InvalidProvider(),))


def test_safe_factory_and_requirement_failure(isolated_registry):
    def factory():
        raise RuntimeError("REJECTED_SENTINEL")

    d.register_diagnostic_provider("broken", factory)
    with pytest.raises(d.DiagnosticContractError) as caught:
        d.diagnostic_providers()
    assert "REJECTED_SENTINEL" not in str(caught.value)
    assert caught.value.__suppress_context__

    class BrokenRequirements(FakeProvider):
        def required_codes(self, ctx):
            raise RuntimeError("REJECTED_SENTINEL")

    with pytest.raises(d.DiagnosticContractError) as caught:
        d.required_check_codes(context(), (BrokenRequirements(),))
    assert "REJECTED_SENTINEL" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_contract_runs_without_django_or_domain_imports():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
class ForbidDomainImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "django" or fullname.startswith(("django.", "linuxlab.sync", "linuxlab.publication")):
            raise AssertionError("Contract imported deployment implementation")
sys.meta_path.insert(0, ForbidDomainImports())
from linuxlab.core.diagnostics import DiagnosticContext, aggregate_diagnostics, report_to_json
report = aggregate_diagnostics(DiagnosticContext("node", "local", frozenset()), ())
print(report_to_json(report))
""",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "profile": "node",
        "mode": "local",
        "overall_status": "PASS",
        "checks": [],
    }
    assert result.stderr == ""


@pytest.mark.parametrize("checks", ["", b"", {}, None, 5, [None]])
def test_direct_report_rejects_non_check_collections(checks):
    with pytest.raises(d.DiagnosticContractError):
        d.DiagnosticReport(1, "node", "local", "PASS", checks)
    with pytest.raises(d.DiagnosticContractError):
        d.aggregate_diagnostics(context(), checks)


@pytest.mark.parametrize("required", [frozenset({"UNKNOWN"}), frozenset({1}), set(), ""])
def test_invalid_required_code_set(required):
    with pytest.raises(d.DiagnosticContractError):
        d.validate_diagnostic_report(wire(), context=context(), required_codes=required)


@pytest.mark.parametrize("text", ["\ud800", "😀" * 513])
def test_invalid_message_unicode_and_length(text):
    with pytest.raises(d.DiagnosticContractError):
        d.DiagnosticCheck("DB_CONNECTIVITY", "PASS", text)


def test_parsed_report_is_detached_and_serialization_sorted():
    payload = wire()
    payload["checks"][0]["details"] = {"error_code": "SAFE"}
    report = validate(payload)
    payload["checks"][0]["message"] = "changed"
    payload["checks"][0]["details"]["error_code"] = "CHANGED"
    assert report.checks[0].message == "Ready"
    assert report.checks[0].details == {"error_code": "SAFE"}
    checks = [d.DiagnosticCheck("TRUST_KEY", "PASS", ""), report.checks[0]]
    direct = d.DiagnosticReport(1, "node", "local", "PASS", checks)
    assert [c["code"] for c in d.report_to_dict(direct)["checks"]] == [
        "DB_CONNECTIVITY",
        "TRUST_KEY",
    ]


@pytest.mark.parametrize(
    "code",
    [
        "DB_CONNECTIVITY",
        "SCHEMA_STATE",
        "SIGNING_KEY",
        "TRUST_KEY",
        "NODE_REGISTRATION",
        "SYNC_RETRY_POLICY",
        "CENTRAL_CONNECTIVITY",
        "CENTRAL_AUTH_ACCEPTANCE",
    ],
)
def test_all_known_codes_roundtrip(code):
    report = d.aggregate_diagnostics(context(), [d.DiagnosticCheck(code, "PASS", "")])
    assert (
        d.validate_diagnostic_report(
            d.report_to_dict(report), context=context(), required_codes=frozenset({code})
        )
        == report
    )


@pytest.mark.parametrize("providers", [iter([FakeProvider()]), {}, "", None])
def test_requirements_reject_non_sequence_providers(providers):
    # A consumed iterator must never silently erase required check codes.
    with pytest.raises(d.DiagnosticContractError):
        d.required_check_codes(context(), providers)


def test_registry_empty_and_invalid_factory_product(isolated_registry):
    assert d.diagnostic_providers() == ()
    assert d.required_check_codes(context(), ()) == frozenset()
    d.register_diagnostic_provider("invalid", lambda: object())
    with pytest.raises(d.DiagnosticContractError):
        d.diagnostic_providers()
