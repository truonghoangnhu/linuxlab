"""Corrective B Task 10: core orchestration (build_diagnostic_context /
diagnose_deployment) and CoreDiagnosticProvider (DB_CONNECTIVITY,
SCHEMA_STATE) over the public Task-2 primitives."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from linuxlab.core import diagnostics as d
from linuxlab.core.diagnostics import (
    DiagnosticCheck,
    DiagnosticContext,
    DiagnosticContractError,
    build_diagnostic_context,
    diagnose_deployment,
)
from linuxlab.core.local_diagnostics import CoreDiagnosticProvider

SENTINEL = "PROVIDER_EXCEPTION_SENTINEL_71ab"


def _settings(profile, **flags):
    values = {
        "LINUXLAB_PROFILE": profile,
        "LINUXLAB_CAN_AUTHOR": False,
        "LINUXLAB_CAN_IMPORT_PACKAGES": False,
        "LINUXLAB_CAN_SERVE_LEARNER_CONTENT": False,
        "LINUXLAB_CAN_RECEIVE_SYNC_EVENTS": False,
        "LINUXLAB_CAN_RUN_SYNC_WORKER": False,
        # Present so a context builder that wrongly parsed sync settings
        # would trip on it.
        "LINUXLAB_SYNC_SECRET": "CONTEXT_SECRET_SENTINEL",
    }
    values.update(flags)
    return SimpleNamespace(**values)


# --- build_diagnostic_context ------------------------------------------------


def test_context_reads_profile_and_exact_enabled_capability_names():
    context = build_diagnostic_context(
        "local",
        settings_obj=_settings(
            "central", LINUXLAB_CAN_AUTHOR=True, LINUXLAB_CAN_RECEIVE_SYNC_EVENTS=True
        ),
    )

    assert context.profile == "central"
    assert context.mode == "local"
    assert context.capabilities == frozenset(
        {"LINUXLAB_CAN_AUTHOR", "LINUXLAB_CAN_RECEIVE_SYNC_EVENTS"}
    )


def test_context_node_profile_capabilities():
    context = build_diagnostic_context(
        "local",
        settings_obj=_settings(
            "node",
            LINUXLAB_CAN_IMPORT_PACKAGES=True,
            LINUXLAB_CAN_SERVE_LEARNER_CONTENT=True,
            LINUXLAB_CAN_RUN_SYNC_WORKER=True,
        ),
    )

    assert context.profile == "node"
    assert context.capabilities == frozenset(
        {
            "LINUXLAB_CAN_IMPORT_PACKAGES",
            "LINUXLAB_CAN_SERVE_LEARNER_CONTENT",
            "LINUXLAB_CAN_RUN_SYNC_WORKER",
        }
    )


@pytest.mark.parametrize("profile", ["test", "staging", "", None, 1, "Central"])
def test_context_rejects_nonproduction_or_unknown_profile_safely(profile):
    with pytest.raises(DiagnosticContractError) as caught:
        build_diagnostic_context("local", settings_obj=_settings(profile))
    assert "staging" not in str(caught.value)
    assert "CONTEXT_SECRET_SENTINEL" not in str(caught.value)


@pytest.mark.parametrize("flag", ["true", 1, "CONTEXT_SECRET_SENTINEL", None])
def test_context_rejects_non_boolean_capability_flags_without_truthiness(flag):
    with pytest.raises(DiagnosticContractError) as caught:
        build_diagnostic_context("local", settings_obj=_settings("node", LINUXLAB_CAN_AUTHOR=flag))
    assert "CONTEXT_SECRET_SENTINEL" not in str(caught.value)


@pytest.mark.parametrize("mode", ["", "LOCAL", "probe", None])
def test_context_rejects_invalid_mode(mode):
    with pytest.raises(DiagnosticContractError):
        build_diagnostic_context(mode, settings_obj=_settings("node"))


def test_context_uses_django_settings_by_default(settings):
    settings.LINUXLAB_PROFILE = "node"
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

    context = build_diagnostic_context("local")

    assert context.profile == "node"
    assert "LINUXLAB_CAN_IMPORT_PACKAGES" in context.capabilities


@pytest.mark.django_db
def test_context_building_performs_no_queries(django_assert_num_queries, settings):
    settings.LINUXLAB_PROFILE = "central"
    with django_assert_num_queries(0):
        build_diagnostic_context("local")


# --- CoreDiagnosticProvider --------------------------------------------------


def _node_context():
    return DiagnosticContext("node", "local", frozenset())


def test_core_provider_owns_and_always_requires_both_checks():
    provider = CoreDiagnosticProvider()
    assert provider.codes == frozenset({"DB_CONNECTIVITY", "SCHEMA_STATE"})
    assert provider.required_codes(_node_context()) == provider.codes
    assert provider.required_codes(DiagnosticContext("central", "local", frozenset())) == (
        provider.codes
    )


@pytest.mark.django_db
def test_core_provider_passes_on_healthy_local_database():
    checks = {c.code: c for c in CoreDiagnosticProvider().checks(_node_context())}
    assert checks["DB_CONNECTIVITY"].status == "PASS"
    assert checks["SCHEMA_STATE"].status == "PASS"


def test_core_provider_fails_schema_when_database_is_unreachable():
    with (
        patch("linuxlab.core.local_diagnostics.check_database", return_value=False),
        patch("linuxlab.core.local_diagnostics.check_schema_state") as schema,
    ):
        checks = {c.code: c for c in CoreDiagnosticProvider().checks(_node_context())}
    assert checks["DB_CONNECTIVITY"].status == "FAIL"
    assert checks["SCHEMA_STATE"].status == "FAIL"
    schema.assert_not_called()


@pytest.mark.django_db
def test_core_provider_fails_only_schema_when_migrations_are_pending():
    with patch("linuxlab.core.local_diagnostics.check_schema_state", return_value=False):
        checks = {c.code: c for c in CoreDiagnosticProvider().checks(_node_context())}
    assert checks["DB_CONNECTIVITY"].status == "PASS"
    assert checks["SCHEMA_STATE"].status == "FAIL"


@pytest.mark.django_db
def test_core_provider_never_runs_migrations():
    with patch("django.db.migrations.executor.MigrationExecutor.migrate") as migrate:
        CoreDiagnosticProvider().checks(_node_context())
    migrate.assert_not_called()


# --- diagnose_deployment orchestration ---------------------------------------


class _StaticProvider:
    codes = frozenset({"TRUST_KEY"})

    def required_codes(self, context):
        return self.codes

    def checks(self, context):
        return (DiagnosticCheck("TRUST_KEY", "PASS", "ok"),)


class _ExplodingProvider:
    codes = frozenset({"SIGNING_KEY"})

    def required_codes(self, context):
        return self.codes

    def checks(self, context):
        raise RuntimeError(SENTINEL)


class _OmittingProvider:
    codes = frozenset({"SIGNING_KEY"})

    def required_codes(self, context):
        return self.codes

    def checks(self, context):
        return ()


def test_diagnose_with_explicit_providers_aggregates_and_validates():
    report = diagnose_deployment(_node_context(), providers=[_StaticProvider()])
    assert report.overall_status == "PASS"
    assert [c.code for c in report.checks] == ["TRUST_KEY"]


def test_unexpected_provider_failure_becomes_fixed_safe_fail_for_required_codes():
    report = diagnose_deployment(
        _node_context(), providers=[_StaticProvider(), _ExplodingProvider()]
    )

    checks = {c.code: c for c in report.checks}
    assert report.overall_status == "FAIL"
    assert checks["SIGNING_KEY"].status == "FAIL"
    assert checks["SIGNING_KEY"].details["error_code"] == "PROVIDER_FAILED"
    assert checks["TRUST_KEY"].status == "PASS"
    rendered = d.report_to_json(report) + repr(report)
    assert SENTINEL not in rendered
    d.validate_diagnostic_report(
        d.report_to_dict(report),
        context=_node_context(),
        required_codes=frozenset({"TRUST_KEY", "SIGNING_KEY"}),
    )


def test_provider_omitting_a_required_code_is_contained_as_fail():
    report = diagnose_deployment(_node_context(), providers=[_OmittingProvider()])
    checks = {c.code: c for c in report.checks}
    assert checks["SIGNING_KEY"].status == "FAIL"
    assert checks["SIGNING_KEY"].details["error_code"] == "PROVIDER_FAILED"


def test_provider_emitting_unowned_code_is_contained_as_fail():
    class _Rogue(_StaticProvider):
        def checks(self, context):
            return (DiagnosticCheck("SIGNING_KEY", "PASS", "not mine"),)

    report = diagnose_deployment(_node_context(), providers=[_Rogue()])
    checks = {c.code: c for c in report.checks}
    assert set(checks) == {"TRUST_KEY"}
    assert checks["TRUST_KEY"].status == "FAIL"


def test_diagnose_resolves_registry_factories_exactly_once(monkeypatch):
    constructed = []

    class _Counted(_StaticProvider):
        def __init__(self):
            constructed.append(self)

    monkeypatch.setattr(d, "_factories", {"counted": _Counted}, raising=False)

    diagnose_deployment(_node_context())

    assert len(constructed) == 1


def test_diagnose_rejects_conflicting_provider_ownership():
    with pytest.raises(DiagnosticContractError):
        diagnose_deployment(_node_context(), providers=[_StaticProvider(), _StaticProvider()])


def test_diagnose_report_passes_canonical_validator_with_same_required_set(monkeypatch):
    seen = {}
    real = d.validate_diagnostic_report

    def _spy(value, *, context, required_codes):
        seen["context"] = context
        seen["required_codes"] = required_codes
        return real(value, context=context, required_codes=required_codes)

    monkeypatch.setattr(d, "validate_diagnostic_report", _spy)
    context = _node_context()

    diagnose_deployment(context, providers=[_StaticProvider()])

    assert seen["context"] is context
    assert seen["required_codes"] == frozenset({"TRUST_KEY"})
