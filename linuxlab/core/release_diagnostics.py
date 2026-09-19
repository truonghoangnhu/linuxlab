"""Canonical release validation (Corrective B Task 12, approved spec §8).

`validate_release_diagnostics()` is the ONE place a release consumer turns
the raw stdout of `diagnose_deployment --mode release --format json` plus
the process return code into an accepted DiagnosticReport -- or fails
closed. It is pure: no Django, no HTTP, no model query, no key/trust
inspection, no human-message parsing and no aggregation or status
recalculation of its own. The required code set is derived from the
canonical context and providers through `required_check_codes()` -- never
trusted from the report -- and the raw text is handed to core's canonical
`validate_diagnostic_report()`, which performs duplicate-member-safe JSON
decoding and every structural/status/consistency rule.

Fails closed (ReleaseDiagnosticsError with fixed safe text, never raw
exception context) on: a context whose mode is not "release", a nonzero
return code, output over 65536 UTF-8 bytes, non-text output, any contract
violation (malformed JSON, duplicate members, wrong schema/profile/mode,
missing/duplicate/invalid code or status, inconsistent overall status),
or an overall FAIL. A valid report whose required checks are PASS/WARN
and whose overall status is PASS or WARN is accepted.
"""

from collections.abc import Sequence

from .diagnostics import (
    DiagnosticContext,
    DiagnosticContractError,
    DiagnosticProvider,
    DiagnosticReport,
    required_check_codes,
    validate_diagnostic_report,
)

_MAX_OUTPUT_BYTES = 65536

_NOT_RELEASE_CONTEXT = "release validation requires a release-mode diagnostic context"
_NONZERO_RETURNCODE = "release diagnostics command did not exit successfully"
_OUTPUT_NOT_TEXT = "release diagnostics output is not text"
_OUTPUT_TOO_LARGE = "release diagnostics output exceeds the canonical report size limit"
_REPORT_INVALID = "release diagnostics output is not a valid canonical report"
_REPORT_FAILED = "release diagnostics reported FAIL"


class ReleaseDiagnosticsError(RuntimeError):
    """Fixed-message release validation failure; never carries raw output,
    exception text, credentials or configuration values."""


def validate_release_diagnostics(
    stdout: str,
    returncode: int,
    *,
    context: DiagnosticContext,
    providers: Sequence[DiagnosticProvider],
) -> DiagnosticReport:
    if not isinstance(context, DiagnosticContext) or context.mode != "release":
        raise ReleaseDiagnosticsError(_NOT_RELEASE_CONTEXT)
    if type(returncode) is not int or returncode != 0:
        raise ReleaseDiagnosticsError(_NONZERO_RETURNCODE)
    if type(stdout) is not str:
        raise ReleaseDiagnosticsError(_OUTPUT_NOT_TEXT)
    try:
        size = len(stdout.encode("utf-8"))
    except UnicodeError:
        raise ReleaseDiagnosticsError(_OUTPUT_NOT_TEXT) from None
    if size > _MAX_OUTPUT_BYTES:
        raise ReleaseDiagnosticsError(_OUTPUT_TOO_LARGE)

    try:
        required = required_check_codes(context, providers)
        report = validate_diagnostic_report(stdout, context=context, required_codes=required)
    except DiagnosticContractError:
        raise ReleaseDiagnosticsError(_REPORT_INVALID) from None

    if report.overall_status == "FAIL":
        raise ReleaseDiagnosticsError(_REPORT_FAILED)
    return report
