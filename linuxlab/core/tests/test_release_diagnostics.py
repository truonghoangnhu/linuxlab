"""Corrective B Task 12: the pure canonical release validator
(linuxlab.core.release_diagnostics). Required codes come from canonical
providers via required_check_codes(), never from the supplied report; the
raw stdout string is decoded by core's duplicate-member-safe validator; any
contract failure, nonzero exit, oversized output, wrong mode/profile or
overall FAIL fails closed as ReleaseDiagnosticsError. No HTTP, no models,
no message parsing, no aggregation of its own."""

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from linuxlab.core.diagnostics import DiagnosticContext
from linuxlab.core.release_diagnostics import (
    ReleaseDiagnosticsError,
    validate_release_diagnostics,
)


class RequiredTrustProvider:
    codes = frozenset({"TRUST_KEY"})

    def required_codes(self, context):
        return self.codes

    def checks(self, context):
        return ()


def test_release_validator_rejects_missing_required_code():
    context = DiagnosticContext("node", "release", frozenset({"LINUXLAB_CAN_IMPORT_PACKAGES"}))
    text = '{"schema_version":1,"profile":"node","mode":"release","overall_status":"PASS","checks":[]}'
    with pytest.raises(ReleaseDiagnosticsError):
        validate_release_diagnostics(text, 0, context=context, providers=(RequiredTrustProvider(),))


# --- boundary coverage ----------------------------------------------------------

NODE_RELEASE = DiagnosticContext("node", "release", frozenset({"LINUXLAB_CAN_IMPORT_PACKAGES"}))
REPORT_SENTINEL = "REPORT_TEXT_SENTINEL_77c1"


class CoreLikeProvider:
    codes = frozenset({"DB_CONNECTIVITY", "SCHEMA_STATE"})

    def required_codes(self, context):
        return self.codes

    def checks(self, context):
        raise AssertionError("the validator must never run provider checks")


class ExplodingRequirementsProvider(RequiredTrustProvider):
    def required_codes(self, context):
        raise RuntimeError(REPORT_SENTINEL)


PROVIDERS = (CoreLikeProvider(), RequiredTrustProvider())


def _report(**overrides):
    checks = overrides.pop(
        "checks",
        [
            {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok", "details": {}},
            {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
            {"code": "TRUST_KEY", "status": "PASS", "message": "ok"},
        ],
    )
    document = {
        "schema_version": 1,
        "profile": "node",
        "mode": "release",
        "overall_status": "PASS",
        "checks": checks,
    }
    document.update(overrides)
    return json.dumps(document)


def _validate(text, returncode=0, *, context=NODE_RELEASE, providers=PROVIDERS):
    return validate_release_diagnostics(text, returncode, context=context, providers=providers)


def test_valid_pass_report_is_accepted():
    report = _validate(_report())
    assert report.overall_status == "PASS"
    assert [c.code for c in report.checks] == ["DB_CONNECTIVITY", "SCHEMA_STATE", "TRUST_KEY"]


def test_warn_only_report_is_accepted():
    text = _report(
        overall_status="WARN",
        checks=[
            {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
            {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
            {"code": "TRUST_KEY", "status": "WARN", "message": "review"},
        ],
    )
    assert _validate(text).overall_status == "WARN"


def test_overall_fail_is_rejected_even_when_structurally_valid():
    text = _report(
        overall_status="FAIL",
        checks=[
            {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
            {"code": "SCHEMA_STATE", "status": "FAIL", "message": "bad"},
            {"code": "TRUST_KEY", "status": "PASS", "message": "ok"},
        ],
    )
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        _validate(text)
    assert str(caught.value) == "release diagnostics reported FAIL"


@pytest.mark.parametrize("returncode", [1, 2, 3, -1, 255, True, None, "0", 0.0])
def test_nonzero_or_non_integer_returncode_is_rejected(returncode):
    with pytest.raises(ReleaseDiagnosticsError):
        _validate(_report(), returncode)


def test_oversized_output_is_rejected_before_decoding():
    padded = _report(checks=[{"code": "DB_CONNECTIVITY", "status": "PASS", "message": "x" * 512}])
    huge = padded[:-1] + " " * (65537 - len(padded)) + "}"
    assert len(huge.encode("utf-8")) > 65536
    with pytest.raises(ReleaseDiagnosticsError):
        _validate(huge)


def test_exact_limit_output_is_still_decoded():
    text = _report()
    padded = text[:-1] + " " * (65536 - len(text)) + "}"
    assert len(padded.encode("utf-8")) == 65536
    assert _validate(padded).overall_status == "PASS"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "null",
        '{"schema_version":1,"profile":"node","mode":"release","overall_status":"PASS","checks":[],"checks":[]}',
        _report(schema_version=2),
        _report(schema_version="1"),
        _report(schema_version=True),
        _report(profile="central"),
        _report(mode="local"),
        _report(mode="remote"),
        _report(overall_status="OK"),
        _report(extra="member"),
        _report(checks=[{"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"}]),
        _report(
            checks=[
                {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
                {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
                {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
                {"code": "TRUST_KEY", "status": "PASS", "message": "ok"},
            ]
        ),
        _report(
            checks=[
                {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
                {"code": "SCHEMA_STATE", "status": "SKIPPED", "message": "ok"},
                {"code": "TRUST_KEY", "status": "PASS", "message": "ok"},
            ]
        ),
        _report(
            checks=[
                {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
                {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
                {"code": "UNKNOWN_CODE", "status": "PASS", "message": "ok"},
                {"code": "TRUST_KEY", "status": "PASS", "message": "ok"},
            ]
        ),
        _report(
            overall_status="PASS",
            checks=[
                {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
                {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
                {"code": "TRUST_KEY", "status": "FAIL", "message": "bad"},
            ],
        ),
    ],
)
def test_contract_violations_are_rejected_with_fixed_safe_text(text):
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        _validate(text)
    assert str(caught.value) in {
        "release diagnostics output is not a valid canonical report",
        "release diagnostics reported FAIL",
    }
    assert caught.value.__suppress_context__ or caught.value.__cause__ is None


def test_bytes_output_is_rejected_not_decoded():
    with pytest.raises(ReleaseDiagnosticsError):
        _validate(_report().encode("utf-8"))


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_non_release_context_is_rejected_before_any_provider_call(mode):
    context = DiagnosticContext("node", mode, frozenset())
    text = _report(mode=mode)
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        _validate(text, context=context, providers=(ExplodingRequirementsProvider(),))
    assert str(caught.value) == "release validation requires a release-mode diagnostic context"


def test_required_codes_come_from_providers_never_from_the_report():
    # The report claims PASS and even lists extra valid codes, but omits
    # the provider-required TRUST_KEY -> rejected.
    text = _report(
        checks=[
            {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
            {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
            {"code": "SIGNING_KEY", "status": "PASS", "message": "ok"},
        ]
    )
    with pytest.raises(ReleaseDiagnosticsError):
        _validate(text)
    # Extra, unrequired but valid codes do not fail validation.
    text = _report(
        checks=[
            {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "ok"},
            {"code": "SCHEMA_STATE", "status": "PASS", "message": "ok"},
            {"code": "TRUST_KEY", "status": "PASS", "message": "ok"},
            {"code": "SIGNING_KEY", "status": "PASS", "message": "ok"},
        ]
    )
    assert _validate(text).overall_status == "PASS"


def test_human_message_text_never_changes_validation():
    a = _validate(_report())
    b = _validate(
        _report(
            checks=[
                {"code": "DB_CONNECTIVITY", "status": "PASS", "message": "FAIL everything"},
                {"code": "SCHEMA_STATE", "status": "PASS", "message": "not ready"},
                {"code": "TRUST_KEY", "status": "PASS", "message": ""},
            ]
        )
    )
    assert a.overall_status == b.overall_status == "PASS"


def test_provider_requirement_failure_is_translated_without_leaking():
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        _validate(_report(), providers=(ExplodingRequirementsProvider(),))
    assert REPORT_SENTINEL not in str(caught.value)
    assert REPORT_SENTINEL not in repr(caught.value)
    assert caught.value.__suppress_context__


def test_error_text_never_echoes_report_content():
    with pytest.raises(ReleaseDiagnosticsError) as caught:
        _validate(_report(profile=REPORT_SENTINEL))
    assert REPORT_SENTINEL not in str(caught.value) + repr(caught.value)


def test_validator_performs_no_network_and_never_runs_checks():
    with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network")):
        _validate(_report())  # CoreLikeProvider.checks would raise if called


def test_validator_module_imports_without_django_or_domain_modules():
    script = """
import importlib.abc, sys
class Forbid(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "django" or fullname.startswith(("django.", "linuxlab.sync", "linuxlab.publication", "urllib.request", "http.client")):
            raise AssertionError("release validator imported " + fullname)
sys.meta_path.insert(0, Forbid())
from linuxlab.core.release_diagnostics import ReleaseDiagnosticsError, validate_release_diagnostics
print("ok")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "ok"
    assert result.stderr == ""
