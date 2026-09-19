"""Corrective B Task 10: `manage.py diagnose_deployment` -- the thin
command adapter over core's canonical diagnostics (approved spec §5).

    manage.py diagnose_deployment [--mode local|remote|release] [--format text|json]

Defaults: --mode local --format text. Task 10 enables LOCAL; Task 11
enables REMOTE for the node profile only (Central REMOTE is rejected
before any provider, configuration load or network call); Task 12 enables
RELEASE on both profiles: `--mode release --format json` is the release
adapter contract, its requirements are derived by the canonical providers
from typed deployment configuration (Node release-probe policy; Central
ignores that Node-only policy), and no CLI switch can override them --
RELEASE is never mapped to REMOTE or LOCAL. Normal Django --settings selects the
deployment: there is no --profile spoof, --skip-check, --no-remote,
--force, policy override, key-path override or remediation flag.

The report is produced exactly once per invocation by diagnose_deployment()
and rendered from that same DiagnosticReport instance: JSON stdout is
exactly one canonical report document (no system-check banner, no
preface, no traceback contamination); text is a human rendering of the
same profile/mode/overall_status/checks, never a second evaluator. PASS
and WARN return normally; a valid FAIL report is written first and then
CommandError(returncode=1) is raised with fixed safe stderr text.
"""

import json
from typing import ClassVar

from django.core.management.base import BaseCommand, CommandError

from linuxlab.core.diagnostics import (
    DiagnosticContractError,
    build_diagnostic_context,
    diagnose_deployment,
    report_to_dict,
    report_to_json,
)

_REMOTE_NODE_ONLY = "remote diagnostics are available only on the node profile"
_REPORT_INVALID = "deployment diagnostics could not produce a valid report"
_REPORT_FAILED = "deployment diagnostics reported FAIL"


def _render_text(report_dict):
    lines = [
        f"profile: {report_dict['profile']}",
        f"mode: {report_dict['mode']}",
        f"overall_status: {report_dict['overall_status']}",
        "checks:",
    ]
    for check in report_dict["checks"]:
        lines.append(f"  {check['code']}: {check['status']} - {check['message']}")
        details = json.dumps(check["details"], sort_keys=True, ensure_ascii=False)
        lines.append(f"    details: {details}")
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Run read-only deployment diagnostics for the active deployment profile."
    requires_system_checks: ClassVar[list[str]] = []

    def create_parser(self, prog_name, subcommand, **kwargs):
        # No prefix abbreviation: an unknown option such as `--force` must
        # fail normally rather than silently matching a longer built-in
        # option (`--force-color`).
        kwargs.setdefault("allow_abbrev", False)
        return super().create_parser(prog_name, subcommand, **kwargs)

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            choices=["local", "remote", "release"],
            default="local",
            help="Diagnostic mode (default: local).",
        )
        parser.add_argument(
            "--format",
            dest="format",
            choices=["text", "json"],
            default="text",
            help="Output format (default: text).",
        )

    def handle(self, *args, **options):
        del args
        mode = options["mode"]
        output_format = options["format"]

        try:
            context = build_diagnostic_context(mode)
        except DiagnosticContractError:
            raise CommandError(_REPORT_INVALID, returncode=2) from None
        if mode == "remote" and context.profile != "node":
            # Task 11: Central refuses REMOTE before any provider check,
            # probe configuration load, probe client or network call.
            raise CommandError(_REMOTE_NODE_ONLY, returncode=2)
        try:
            report = diagnose_deployment(context)
        except DiagnosticContractError:
            raise CommandError(_REPORT_INVALID, returncode=2) from None

        if output_format == "json":
            self.stdout.write(report_to_json(report))
        else:
            self.stdout.write(_render_text(report_to_dict(report)))

        if report.overall_status == "FAIL":
            raise CommandError(_REPORT_FAILED, returncode=1)
