"""Publication's own diagnostic provider (Corrective B Task 10, approved
spec §4): SIGNING_KEY, required only when LINUXLAB_CAN_AUTHOR is enabled,
over the unchanged fixed-path load_package_signing_private_key() loader.
Structural validity only -- a readable, parseable Ed25519 private key is
PASS; LOCAL mode never compares it to any Node trust anchor or remote
service. Only the loader's own typed PackageSigningConfigurationError is
translated (to fixed safe evidence, never the loader's path-bearing
message); anything unexpected is the orchestrator's containment job. No
private-key material, path or PEM text ever reaches a check.
"""

from linuxlab.core.diagnostics import DiagnosticCheck, DiagnosticContext

from .signing import PackageSigningConfigurationError, load_package_signing_private_key

_SIGNING_KEY_INVALID = {"error_code": "SIGNING_KEY_INVALID"}


class PublicationDiagnosticProvider:
    codes = frozenset({"SIGNING_KEY"})

    def required_codes(self, context: DiagnosticContext) -> frozenset[str]:
        if "LINUXLAB_CAN_AUTHOR" in context.capabilities:
            return self.codes
        return frozenset()

    def checks(self, context: DiagnosticContext) -> tuple[DiagnosticCheck, ...]:
        if not self.required_codes(context):
            return ()
        try:
            load_package_signing_private_key()
        except PackageSigningConfigurationError:
            return (
                DiagnosticCheck(
                    "SIGNING_KEY",
                    "FAIL",
                    "Central package signing key is missing or is not a valid Ed25519 private key.",
                    _SIGNING_KEY_INVALID,
                ),
            )
        return (
            DiagnosticCheck(
                "SIGNING_KEY", "PASS", "Central package signing key is readable and valid."
            ),
        )
