"""Corrective B Task 10: `manage.py diagnose_deployment` LOCAL command
adapter over core's diagnose_deployment() orchestration (approved spec
§§4-5). LOCAL mode makes zero Central calls, performs zero persistent
mutation, and JSON stdout is exactly one canonical report."""

import io
import json
import urllib.request
import uuid
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from django.apps import apps as django_apps
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError

from linuxlab.core import diagnostics as d
from linuxlab.core.diagnostics import build_diagnostic_context, diagnostic_providers
from linuxlab.core.local_diagnostics import CoreDiagnosticProvider
from linuxlab.core.models import DomainEvent
from linuxlab.core.release_diagnostics import (
    ReleaseDiagnosticsError,
    validate_release_diagnostics,
)
from linuxlab.sync.authentication import issue_node_registration
from linuxlab.sync.models import (
    REGISTRATION_ACTIVE,
    InboxMessage,
    NodeRegistration,
    OutboxMessage,
    _insert_immutable_row,
)
from linuxlab.sync.probe import CentralProbeResult
from linuxlab.sync.transports import _TRANSPORT_OPENER

# Captured at import time, before any fixture runs, so the teardown-proof
# test at the end of this module can compare against the real originals.
_ORIGINAL_OPENER_OPEN = urllib.request.OpenerDirector.open
_ORIGINAL_URLOPEN = urllib.request.urlopen

pytestmark = pytest.mark.django_db

NODE_SECRET_SENTINEL = "NODE_SECRET_SENTINEL_9f3c1a"


def _write_trust_key(tmp_path):
    key_path = tmp_path / "package-signing-public.key"
    public_key = Ed25519PrivateKey.generate().public_key()
    key_path.write_bytes(public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    return key_path


def _poison_network(monkeypatch):
    """Zero-network poison. Patching the CLASS method OpenerDirector.open is
    what every opener instance -- including the transport's private
    _TRANSPORT_OPENER -- resolves `.open` through, so the instance is never
    patched separately (Fix Round 1, T10-I2): an instance-level monkeypatch
    layered over the class patch would record the already-poisoned bound
    method as its "original" and restore it as an instance shadow attribute
    that outlives the test."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("LOCAL diagnostics must never call Central")

    assert "open" not in vars(_TRANSPORT_OPENER)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", _forbidden)
    monkeypatch.setattr("urllib.request.urlopen", _forbidden)


def _persistent_rows():
    return {
        "registrations": list(NodeRegistration.objects.order_by("pk").values()),
        "outbox": list(OutboxMessage.objects.order_by("pk").values()),
        "inbox": list(InboxMessage.objects.order_by("pk").values()),
        "events": list(DomainEvent.objects.order_by("pk").values()),
    }


@pytest.fixture
def node_local(settings, monkeypatch, tmp_path):
    """A real local Node deployment: node profile, the Node capability
    flags exactly as linuxlab/settings/node.py defines them, outbound sync
    enabled with a valid typed identity/secret/URL and the base retry
    configuration, and a fixture Ed25519 trust anchor on the unchanged
    fixed trust-path seam."""
    settings.LINUXLAB_PROFILE = "node"
    settings.LINUXLAB_CAN_AUTHOR = False
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
    settings.LINUXLAB_CAN_SERVE_LEARNER_CONTENT = True
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = str(uuid.uuid4())
    settings.LINUXLAB_SYNC_SECRET = NODE_SECRET_SENTINEL
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = False
    key_path = _write_trust_key(tmp_path)
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
    _poison_network(monkeypatch)
    return {"trust_key_path": key_path}


def test_local_node_credentials_are_unverified_warn(node_local):
    before = _persistent_rows()
    key_bytes_before = node_local["trust_key_path"].read_bytes()

    out = io.StringIO()
    call_command("diagnose_deployment", "--mode", "local", "--format", "json", stdout=out)

    report = json.loads(out.getvalue())
    checks = {check["code"]: check for check in report["checks"]}

    assert checks["NODE_REGISTRATION"]["status"] == "WARN"
    assert checks["NODE_REGISTRATION"]["details"]["authority_verified"] is False
    assert "CENTRAL_CONNECTIVITY" not in checks
    assert report["overall_status"] == "WARN"

    assert _persistent_rows() == before
    assert node_local["trust_key_path"].read_bytes() == key_bytes_before
    assert NODE_SECRET_SENTINEL not in out.getvalue()


# --- shared helpers ---------------------------------------------------------

HASH_SENTINEL = "!HASH_SENTINEL_unusable_7c2d"
RAW_BODY_SENTINEL = 'HTTP 500 body {"token": "RAW_BODY_SENTINEL_e0a1"}'
AUTHORIZATION_SENTINEL = "Authorization: Bearer AUTHZ_SENTINEL_5e77"
CONFIG_SENTINEL = "CONFIG_VALUE_SENTINEL_3b9d"

_MUTATION_TARGETS = (
    "linuxlab.sync.authentication.issue_node_registration",
    "linuxlab.sync.authentication.rotate_node_credential",
    "linuxlab.sync.authentication.revoke_node_registration",
    "linuxlab.sync.outbox.requeue_outbox_message",
    "linuxlab.sync.outbox.flush_outbox",
    "linuxlab.core.events.publish_event",
)


def _run(*argv):
    out, err = io.StringIO(), io.StringIO()
    call_command("diagnose_deployment", *argv, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def _run_expecting_failure(*argv):
    out, err = io.StringIO(), io.StringIO()
    with pytest.raises(CommandError) as caught:
        call_command("diagnose_deployment", *argv, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue(), caught.value


def _json_report(stdout):
    # Exactly one JSON document: parse the whole stdout in one go.
    return json.loads(stdout)


def _parse_text(text):
    """Test-only parser of the human rendering, to compare semantics
    against the JSON rendering; production never parses text."""
    lines = text.rstrip("\n").split("\n")
    header = dict(line.split(": ", 1) for line in lines[:3])
    assert lines[3] == "checks:"
    checks = {}
    body = lines[4:]
    for index in range(0, len(body), 2):
        code, rest = body[index].strip().split(": ", 1)
        status, message = rest.split(" - ", 1)
        details = json.loads(body[index + 1].strip().removeprefix("details: "))
        checks[code] = {"code": code, "status": status, "message": message, "details": details}
    return header, checks


@pytest.fixture
def central_local(settings, monkeypatch, tmp_path):
    """A real local Central deployment: central profile with the Central
    capability flags exactly as linuxlab/settings/central.py defines them,
    and a fixture Ed25519 signing key on the unchanged fixed-path seam."""
    settings.LINUXLAB_PROFILE = "central"
    settings.LINUXLAB_CAN_AUTHOR = True
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
    settings.LINUXLAB_CAN_SERVE_LEARNER_CONTENT = False
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = False
    settings.LINUXLAB_SYNC_ENABLED = False
    settings.LINUXLAB_NODE_ID = None
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_CENTRAL_URL = None
    key_path = tmp_path / "package-signing.key"
    pem = Ed25519PrivateKey.generate().private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    key_path.write_bytes(pem)
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)
    _poison_network(monkeypatch)
    return {"signing_key_path": key_path, "private_material": pem.decode().splitlines()[1]}


# --- LOCAL selection matrices via the real command ---------------------------


def test_central_local_matrix(central_local):
    stdout, _ = _run("--mode", "local", "--format", "json")
    report = _json_report(stdout)
    checks = {c["code"]: c for c in report["checks"]}

    assert report["profile"] == "central"
    assert report["mode"] == "local"
    assert set(checks) == {"DB_CONNECTIVITY", "SCHEMA_STATE", "SIGNING_KEY", "NODE_REGISTRATION"}
    assert checks["SIGNING_KEY"]["status"] == "PASS"
    assert checks["NODE_REGISTRATION"]["status"] == "PASS"
    assert checks["NODE_REGISTRATION"]["details"] == {"node_count": 0, "invalid_count": 0}
    assert report["overall_status"] == "PASS"


def test_node_local_matrix(node_local):
    stdout, _ = _run("--mode", "local", "--format", "json")
    report = _json_report(stdout)
    checks = {c["code"]: c for c in report["checks"]}

    assert report["profile"] == "node"
    assert set(checks) == {
        "DB_CONNECTIVITY",
        "SCHEMA_STATE",
        "TRUST_KEY",
        "NODE_REGISTRATION",
        "SYNC_RETRY_POLICY",
    }
    assert checks["TRUST_KEY"]["status"] == "PASS"
    assert checks["SYNC_RETRY_POLICY"]["status"] == "PASS"
    assert "SIGNING_KEY" not in checks
    assert "CENTRAL_AUTH_ACCEPTANCE" not in checks


def test_node_with_disabled_capabilities_runs_only_core_checks(node_local, settings):
    settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
    settings.LINUXLAB_SYNC_ENABLED = False

    stdout, _ = _run("--format", "json")

    report = _json_report(stdout)
    assert {c["code"] for c in report["checks"]} == {"DB_CONNECTIVITY", "SCHEMA_STATE"}
    assert report["overall_status"] == "PASS"


def test_central_with_receive_disabled_omits_registration(central_local, settings):
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
    stdout, _ = _run("--format", "json")
    assert {c["code"] for c in _json_report(stdout)["checks"]} == {
        "DB_CONNECTIVITY",
        "SCHEMA_STATE",
        "SIGNING_KEY",
    }


def test_central_never_reports_node_sync_policy(central_local, settings):
    settings.LINUXLAB_SYNC_ENABLED = CONFIG_SENTINEL
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = CONFIG_SENTINEL
    stdout, _ = _run("--format", "json")
    report = _json_report(stdout)
    assert "SYNC_RETRY_POLICY" not in {c["code"] for c in report["checks"]}
    assert report["overall_status"] == "PASS"
    assert CONFIG_SENTINEL not in stdout


def test_disabled_node_with_malformed_require_probe_never_passes(node_local, settings):
    # Fix Round 1 (T10-I1): outbound sync typed False, but the Node release-
    # probe policy is malformed -- with healthy core diagnostics this must
    # still be a policy FAIL, never a silent PASS, and NODE_REGISTRATION is
    # not inferred from it.
    settings.LINUXLAB_SYNC_ENABLED = False
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = CONFIG_SENTINEL
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_CENTRAL_URL = None
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = False
    before = _persistent_rows()

    stdout, stderr, error = _run_expecting_failure("--mode", "local", "--format", "json")

    report = _json_report(stdout)
    checks = {c["code"]: c for c in report["checks"]}
    assert checks["DB_CONNECTIVITY"]["status"] == "PASS"
    assert checks["SCHEMA_STATE"]["status"] == "PASS"
    assert checks["SYNC_RETRY_POLICY"]["status"] == "FAIL"
    assert checks["SYNC_RETRY_POLICY"]["details"]["error_code"] == "SYNC_CONFIG_INVALID_VALUE"
    assert "NODE_REGISTRATION" not in checks
    assert report["overall_status"] == "FAIL"
    assert error.returncode == 1
    everything = stdout + stderr + str(error)
    assert CONFIG_SENTINEL not in everything
    assert "Traceback" not in everything
    assert _persistent_rows() == before


# --- defaults, JSON contract, text/JSON semantic equality -------------------


def test_defaults_are_local_text(node_local):
    stdout, stderr = _run()
    header, checks = _parse_text(stdout)
    assert header == {"profile": "node", "mode": "local", "overall_status": "WARN"}
    assert checks["NODE_REGISTRATION"]["status"] == "WARN"
    assert stderr == ""
    with pytest.raises(ValueError):
        json.loads(stdout)


def test_json_stdout_is_exactly_one_canonical_report(node_local):
    stdout, stderr = _run("--format", "json")

    document = stdout.strip()
    assert document.startswith("{") and document.endswith("}")
    assert stdout.count("\n") <= 1
    report = json.loads(document)
    assert set(report) == {"schema_version", "profile", "mode", "overall_status", "checks"}
    assert "System check" not in stdout
    assert "Traceback" not in stdout
    assert stderr == ""
    context = d.DiagnosticContext("node", "local", frozenset({"LINUXLAB_CAN_IMPORT_PACKAGES"}))
    d.validate_diagnostic_report(document, context=context, required_codes=frozenset())


def test_text_and_json_render_the_same_report(node_local):
    json_stdout, _ = _run("--format", "json")
    text_stdout, _ = _run("--format", "text")

    report = _json_report(json_stdout)
    header, text_checks = _parse_text(text_stdout)
    assert header["profile"] == report["profile"]
    assert header["mode"] == report["mode"]
    assert header["overall_status"] == report["overall_status"]
    assert text_checks == {c["code"]: c for c in report["checks"]}


def test_command_runs_diagnosis_exactly_once_per_invocation(node_local, monkeypatch):
    calls = []
    real = d.diagnose_deployment

    def _counting(context, **kwargs):
        calls.append(context)
        return real(context, **kwargs)

    monkeypatch.setattr(
        "linuxlab.core.management.commands.diagnose_deployment.diagnose_deployment", _counting
    )
    _run("--format", "text")
    assert len(calls) == 1


# --- PASS / WARN / FAIL exit behaviour --------------------------------------


def test_pass_returns_normally(central_local):
    stdout, stderr = _run("--format", "json")
    assert _json_report(stdout)["overall_status"] == "PASS"
    assert stderr == ""


def test_warn_returns_normally(node_local):
    stdout, stderr = _run("--format", "json")
    assert _json_report(stdout)["overall_status"] == "WARN"
    assert stderr == ""


def test_fail_emits_valid_report_then_returncode_1(node_local, monkeypatch, tmp_path):
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "missing.key")
    before = _persistent_rows()

    stdout, stderr, error = _run_expecting_failure("--format", "json")

    report = _json_report(stdout)
    checks = {c["code"]: c for c in report["checks"]}
    assert report["overall_status"] == "FAIL"
    assert checks["TRUST_KEY"]["status"] == "FAIL"
    assert error.returncode == 1
    assert str(error) == "deployment diagnostics reported FAIL"
    assert str(tmp_path) not in stdout + stderr + str(error)
    assert _persistent_rows() == before


def test_fail_in_text_format_also_returns_1(node_local, monkeypatch, tmp_path):
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "missing.key")
    stdout, _, error = _run_expecting_failure("--format", "text")
    header, checks = _parse_text(stdout)
    assert header["overall_status"] == "FAIL"
    assert checks["TRUST_KEY"]["status"] == "FAIL"
    assert error.returncode == 1


def test_database_failure_fails_both_core_checks_via_command(node_local):
    with patch("linuxlab.core.local_diagnostics.check_database", return_value=False):
        stdout, _, error = _run_expecting_failure("--format", "json")
    checks = {c["code"]: c for c in _json_report(stdout)["checks"]}
    assert checks["DB_CONNECTIVITY"]["status"] == "FAIL"
    assert checks["SCHEMA_STATE"]["status"] == "FAIL"
    assert error.returncode == 1


# --- unavailable modes / invalid invocation ---------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("--profile", "central"),
        ("--skip-check", "TRUST_KEY"),
        ("--no-remote",),
        ("--force",),
        ("--key-path", "/tmp/x"),
        ("--mode", "probe"),
        ("--format", "yaml"),
        ("extra-positional",),
    ],
)
def test_unknown_or_forbidden_options_fail_normally(node_local, argv):
    with patch("linuxlab.core.management.commands.diagnose_deployment.diagnose_deployment") as run:
        out = io.StringIO()
        with pytest.raises((CommandError, SystemExit)):
            call_command("diagnose_deployment", *argv, stdout=out)
    run.assert_not_called()
    assert out.getvalue() == ""


def test_nonproduction_profile_is_rejected_without_a_false_report(node_local, settings):
    settings.LINUXLAB_PROFILE = "test"
    stdout, _stderr, error = _run_expecting_failure("--format", "json")
    assert stdout == ""
    assert error.returncode == 2
    assert str(error) == "deployment diagnostics could not produce a valid report"


# --- provider failure containment through the command -----------------------


class _ExplodingProvider:
    codes = frozenset({"CENTRAL_CONNECTIVITY"})

    def required_codes(self, context):
        return self.codes

    def checks(self, context):
        raise RuntimeError(RAW_BODY_SENTINEL)


class _ExplodingSigningProvider(_ExplodingProvider):
    """Stands in for the real publication factory in a REAL registry
    (Task 11: every one of the eight codes is now owned by a real
    provider, so a fake must replace an owner rather than add a code)."""

    codes = frozenset({"SIGNING_KEY"})


def test_unexpected_provider_failure_is_contained_in_command_output(
    node_local, monkeypatch, caplog
):
    monkeypatch.setattr(
        d, "_factories", {"core": CoreDiagnosticProvider, "zz_fake": _ExplodingProvider}
    )

    stdout, stderr, error = _run_expecting_failure("--format", "json")

    report = _json_report(stdout)
    checks = {c["code"]: c for c in report["checks"]}
    assert checks["CENTRAL_CONNECTIVITY"]["status"] == "FAIL"
    assert checks["CENTRAL_CONNECTIVITY"]["details"] == {"error_code": "PROVIDER_FAILED"}
    assert report["overall_status"] == "FAIL"
    assert error.returncode == 1
    everything = stdout + stderr + str(error) + repr(error) + caplog.text
    assert "RAW_BODY_SENTINEL" not in everything
    assert "Traceback" not in everything
    d.validate_diagnostic_report(
        stdout.strip(),
        context=d.DiagnosticContext("node", "local", frozenset({"LINUXLAB_CAN_IMPORT_PACKAGES"})),
        required_codes=frozenset({"DB_CONNECTIVITY", "SCHEMA_STATE", "CENTRAL_CONNECTIVITY"}),
    )


# --- no-mutation contract ----------------------------------------------------


def test_successful_diagnosis_makes_no_persistent_mutation(central_local):
    credential = issue_node_registration("Lab")
    before = _persistent_rows()
    key_before = central_local["signing_key_path"].read_bytes()
    sessions_before = Session.objects.count()

    from contextlib import ExitStack

    with ExitStack() as stack:
        spies = [
            stack.enter_context(patch(target, side_effect=AssertionError("mutation")))
            for target in _MUTATION_TARGETS
        ]
        stack.enter_context(patch("django.db.migrations.executor.MigrationExecutor.migrate"))
        stdout, _ = _run("--format", "json")

    for spy in spies:
        spy.assert_not_called()
    assert _json_report(stdout)["overall_status"] == "PASS"
    assert _persistent_rows() == before
    assert central_local["signing_key_path"].read_bytes() == key_before
    assert Session.objects.count() == sessions_before
    assert credential.secret not in stdout


def test_failing_diagnosis_makes_no_persistent_mutation(central_local, monkeypatch, tmp_path):
    _insert_immutable_row(
        NodeRegistration,
        node_id=uuid.uuid4(),
        display_name="Corrupt",
        state=REGISTRATION_ACTIVE,
        current_secret_hash=HASH_SENTINEL,
        previous_secret_hash="",
        previous_valid_until=None,
        enabled=True,
    )
    monkeypatch.setattr(
        "linuxlab.publication.signing._SIGNING_KEY_PATH", tmp_path / "missing-signing.key"
    )
    before = _persistent_rows()

    from contextlib import ExitStack

    with ExitStack() as stack:
        spies = [
            stack.enter_context(patch(target, side_effect=AssertionError("mutation")))
            for target in _MUTATION_TARGETS
        ]
        stdout, _stderr, error = _run_expecting_failure("--format", "json")

    for spy in spies:
        spy.assert_not_called()
    checks = {c["code"]: c for c in _json_report(stdout)["checks"]}
    assert checks["NODE_REGISTRATION"]["status"] == "FAIL"
    assert checks["SIGNING_KEY"]["status"] == "FAIL"
    assert error.returncode == 1
    assert _persistent_rows() == before
    assert not (tmp_path / "missing-signing.key").exists()


# --- secret / sentinel boundary ----------------------------------------------


def test_sentinels_never_leak_through_any_output_channel(
    node_local, settings, monkeypatch, caplog, tmp_path
):
    # Node secret sentinel is already configured by the fixture; add the
    # remaining sentinel-bearing inputs: an Authorization-like URL (invalid,
    # so SYNC_RETRY_POLICY fails), a malformed typed retry setting, a
    # malformed trust PEM, and an unexpected provider raising raw-body text.
    settings.LINUXLAB_CENTRAL_URL = f"https://central.example.org/#{AUTHORIZATION_SENTINEL}"
    settings.LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS = CONFIG_SENTINEL
    trust_path = tmp_path / "trust.key"
    trust_path.write_bytes(b"TRUST_PEM_SENTINEL")
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", trust_path)
    real_factories = dict(d._factories)
    monkeypatch.setattr(
        d, "_factories", {**real_factories, "publication": _ExplodingSigningProvider}
    )

    stdout, stderr, error = _run_expecting_failure("--format", "json")
    text_stdout, text_stderr, text_error = _run_expecting_failure("--format", "text")

    everything = "".join(
        [stdout, stderr, str(error), repr(error), text_stdout, text_stderr, str(text_error),
         caplog.text]
    )
    for sentinel in (
        NODE_SECRET_SENTINEL,
        "AUTHZ_SENTINEL",
        "Bearer",
        CONFIG_SENTINEL,
        "TRUST_PEM_SENTINEL",
        "RAW_BODY_SENTINEL",
        str(tmp_path),
    ):
        assert sentinel not in everything, sentinel
    report = _json_report(stdout)
    for check in report["checks"]:
        assert NODE_SECRET_SENTINEL not in check["message"]
        assert NODE_SECRET_SENTINEL not in json.dumps(check["details"])
    assert not OutboxMessage.objects.exists()


def test_central_sentinels_never_leak(central_local, caplog):
    _insert_immutable_row(
        NodeRegistration,
        node_id=uuid.uuid4(),
        display_name="Corrupt",
        state=REGISTRATION_ACTIVE,
        current_secret_hash=HASH_SENTINEL,
        previous_secret_hash="",
        previous_valid_until=None,
        enabled=True,
    )

    stdout, stderr, error = _run_expecting_failure("--format", "json")

    everything = stdout + stderr + str(error) + caplog.text
    assert "HASH_SENTINEL" not in everything
    assert central_local["private_material"] not in everything
    assert "PRIVATE KEY" not in everything
    assert str(central_local["signing_key_path"]) not in everything
    rows = _persistent_rows()["registrations"]
    assert rows[0]["current_secret_hash"] == HASH_SENTINEL  # DB failure fields untouched


# --- startup registration: factories only, zero side effects -----------------


@pytest.mark.parametrize(
    "app_label,name,provider_path",
    [
        ("core", "core", "linuxlab.core.local_diagnostics.CoreDiagnosticProvider"),
        (
            "publication",
            "publication",
            "linuxlab.publication.diagnostics.PublicationDiagnosticProvider",
        ),
        ("sync", "sync", "linuxlab.sync.diagnostics.SyncDiagnosticProvider"),
    ],
)
def test_app_ready_registers_factory_class_without_side_effects(
    monkeypatch, django_assert_num_queries, app_label, name, provider_path
):
    monkeypatch.setattr(d, "_factories", {})
    module_path, class_name = provider_path.rsplit(".", 1)
    import importlib

    provider_class = getattr(importlib.import_module(module_path), class_name)

    def _forbidden(*args, **kwargs):
        raise AssertionError("startup must not instantiate providers, read keys or call out")

    monkeypatch.setattr(provider_class, "__init__", _forbidden)
    monkeypatch.setattr("linuxlab.publication.signing.load_package_signing_private_key", _forbidden)
    monkeypatch.setattr("linuxlab.sync.trust.load_package_trusted_public_key", _forbidden)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", _forbidden)

    with django_assert_num_queries(0):
        django_apps.get_app_config(app_label).ready()

    assert d._factories == {name: provider_class}


def test_sync_ready_retains_enqueue_contract_registration(monkeypatch):
    from linuxlab.sync.registry import get_current_sync_event_contract

    monkeypatch.setattr(d, "_factories", {})
    django_apps.get_app_config("sync").ready()
    assert get_current_sync_event_contract("activity.completed") is not None
    assert "sync" in d._factories


def test_real_startup_registry_holds_the_three_owner_factories():
    from linuxlab.publication.diagnostics import PublicationDiagnosticProvider
    from linuxlab.sync.diagnostics import SyncDiagnosticProvider

    assert d._factories["core"] is CoreDiagnosticProvider
    assert d._factories["publication"] is PublicationDiagnosticProvider
    assert d._factories["sync"] is SyncDiagnosticProvider


# --- report limits ----------------------------------------------------------


def test_report_stays_within_contract_limits_for_a_large_inventory(central_local):
    for index in range(40):
        issue_node_registration(f"Lab {index}")

    stdout, _ = _run("--format", "json")

    assert len(stdout.encode("utf-8")) <= 65536
    report = _json_report(stdout)
    for check in report["checks"]:
        assert len(check["message"]) <= 512
        assert len(json.dumps(check["details"]).encode("utf-8")) <= 4096
    registration = {c["code"]: c for c in report["checks"]}["NODE_REGISTRATION"]
    assert registration["details"] == {"node_count": 40, "invalid_count": 0}


# --- Fix Round 1 (T10-I2): fixture teardown leaves global state exact -------
# Runs last in this module (file order), after every fixture-using test.


def test_network_poison_fixture_leaves_no_opener_state_behind():
    assert "open" not in vars(_TRANSPORT_OPENER)
    assert urllib.request.OpenerDirector.open is _ORIGINAL_OPENER_OPEN
    assert urllib.request.urlopen is _ORIGINAL_URLOPEN


def test_network_poison_is_still_effective_for_the_transport_opener(monkeypatch):
    _poison_network(monkeypatch)
    with pytest.raises(AssertionError, match="never call Central"):
        _TRANSPORT_OPENER.open("https://central.example.org/api/v1/sync/events/")


# --- Corrective B Task 11: REMOTE mode (Node only) ---------------------------


_ACCEPTED_PROBE = CentralProbeResult(reachable=True, accepted=True, http_status=200)
_UNREACHABLE_PROBE = CentralProbeResult(
    reachable=False, accepted=False, error_code="PROBE_UNREACHABLE"
)


class _ProbeSpy:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, credentials, *, opener=None):
        self.calls.append(credentials)
        return self.result


@pytest.fixture
def probe(monkeypatch):
    spy = _ProbeSpy(_ACCEPTED_PROBE)
    monkeypatch.setattr("linuxlab.sync.diagnostics.probe_central", spy)
    return spy


def test_node_remote_json_reports_both_remote_checks_and_keeps_local_warn(node_local, probe):
    before = _persistent_rows()

    from contextlib import ExitStack

    with ExitStack() as stack:
        spies = [
            stack.enter_context(patch(target, side_effect=AssertionError("mutation")))
            for target in _MUTATION_TARGETS
        ]
        stdout, stderr = _run("--mode", "remote", "--format", "json")

    for spy in spies:
        spy.assert_not_called()
    report = _json_report(stdout)
    checks = {c["code"]: c for c in report["checks"]}
    assert report["mode"] == "remote"
    assert set(checks) == {
        "DB_CONNECTIVITY",
        "SCHEMA_STATE",
        "TRUST_KEY",
        "NODE_REGISTRATION",
        "SYNC_RETRY_POLICY",
        "CENTRAL_CONNECTIVITY",
        "CENTRAL_AUTH_ACCEPTANCE",
    }
    assert checks["CENTRAL_CONNECTIVITY"]["status"] == "PASS"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"]["status"] == "PASS"
    assert checks["NODE_REGISTRATION"]["status"] == "WARN"
    assert checks["NODE_REGISTRATION"]["details"]["authority_verified"] is False
    assert report["overall_status"] == "WARN"
    assert stderr == ""
    assert len(probe.calls) == 1
    assert NODE_SECRET_SENTINEL not in stdout
    assert _persistent_rows() == before


def test_node_remote_text_renders_the_same_remote_report(node_local, probe):
    json_stdout, _ = _run("--mode", "remote", "--format", "json")
    text_stdout, _ = _run("--mode", "remote", "--format", "text")
    report = _json_report(json_stdout)
    header, text_checks = _parse_text(text_stdout)
    assert header["mode"] == "remote"
    assert text_checks == {c["code"]: c for c in report["checks"]}
    assert len(probe.calls) == 2  # exactly one probe per command invocation


def test_node_remote_unreachable_central_emits_report_then_returncode_1(node_local, probe):
    probe.result = _UNREACHABLE_PROBE
    before = _persistent_rows()

    stdout, _stderr, error = _run_expecting_failure("--mode", "remote", "--format", "json")

    checks = {c["code"]: c for c in _json_report(stdout)["checks"]}
    assert checks["CENTRAL_CONNECTIVITY"]["status"] == "FAIL"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"]["status"] == "FAIL"
    assert error.returncode == 1
    assert _persistent_rows() == before


def test_node_remote_missing_probe_config_fails_both_with_zero_network(node_local, settings, probe):
    settings.LINUXLAB_SYNC_ENABLED = False
    settings.LINUXLAB_CENTRAL_URL = None

    stdout, _stderr, error = _run_expecting_failure("--mode", "remote", "--format", "json")

    checks = {c["code"]: c for c in _json_report(stdout)["checks"]}
    assert checks["CENTRAL_CONNECTIVITY"]["details"] == {"error_code": "CONFIG_INVALID"}
    assert checks["CENTRAL_AUTH_ACCEPTANCE"]["details"] == {"error_code": "CONFIG_INVALID"}
    assert error.returncode == 1
    assert probe.calls == []


def test_central_remote_is_rejected_before_any_check_or_network(central_local, probe):
    with (
        patch("linuxlab.core.management.commands.diagnose_deployment.diagnose_deployment") as run,
        patch("linuxlab.sync.configuration.load_sync_configuration") as config,
    ):
        stdout, _stderr, error = _run_expecting_failure("--mode", "remote", "--format", "json")
    run.assert_not_called()
    config.assert_not_called()
    assert probe.calls == []
    assert stdout == ""
    assert error.returncode == 2
    assert str(error) == "remote diagnostics are available only on the node profile"


def test_local_mode_still_never_probes(node_local, probe):
    stdout, _ = _run("--mode", "local", "--format", "json")
    assert "CENTRAL_CONNECTIVITY" not in {c["code"] for c in _json_report(stdout)["checks"]}
    assert probe.calls == []


# --- Corrective B Task 12: RELEASE mode (both profiles; policy decides) -------

_REJECTED_PROBE = CentralProbeResult(
    reachable=True, accepted=False, http_status=401, error_code="AUTH_REJECTED"
)
_REMOTE = {"CENTRAL_CONNECTIVITY", "CENTRAL_AUTH_ACCEPTANCE"}


def _validate_release_stdout(stdout, returncode=0):
    return validate_release_diagnostics(
        stdout, returncode, context=build_diagnostic_context("release"), providers=diagnostic_providers()
    )


def test_node_release_policy_false_offline_passes_required_local_checks(node_local, settings, probe):
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = False
    before = _persistent_rows()

    stdout, stderr = _run("--mode", "release", "--format", "json")

    report = _json_report(stdout)
    checks = {c["code"]: c for c in report["checks"]}
    assert report["mode"] == "release"
    assert set(checks) == {
        "DB_CONNECTIVITY", "SCHEMA_STATE", "TRUST_KEY", "NODE_REGISTRATION", "SYNC_RETRY_POLICY"
    }
    assert checks["NODE_REGISTRATION"]["status"] == "WARN"
    assert report["overall_status"] == "WARN"
    assert stderr == ""
    assert probe.calls == []  # policy false: neither remote check runs
    assert _persistent_rows() == before
    # The very same stdout passes the canonical release validator (WARN allowed).
    assert _validate_release_stdout(stdout).overall_status == "WARN"


def test_node_release_policy_false_with_sync_disabled_needs_no_credentials(node_local, settings, probe):
    settings.LINUXLAB_SYNC_ENABLED = False
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = False
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_CENTRAL_URL = None

    stdout, _ = _run("--mode", "release", "--format", "json")

    report = _json_report(stdout)
    assert {c["code"] for c in report["checks"]} == {"DB_CONNECTIVITY", "SCHEMA_STATE", "TRUST_KEY"}
    assert report["overall_status"] == "PASS"
    assert probe.calls == []
    assert _validate_release_stdout(stdout).overall_status == "PASS"


def test_node_release_policy_true_requires_and_runs_one_probe(node_local, settings, probe):
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = True

    stdout, _ = _run("--mode", "release", "--format", "json")

    report = _json_report(stdout)
    checks = {c["code"]: c for c in report["checks"]}
    assert _REMOTE <= set(checks)
    assert checks["CENTRAL_CONNECTIVITY"]["status"] == "PASS"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"]["status"] == "PASS"
    assert checks["NODE_REGISTRATION"]["status"] == "WARN"
    assert len(probe.calls) == 1
    assert _validate_release_stdout(stdout).overall_status == "WARN"


def test_node_release_policy_true_offline_fails_and_readiness_stays_up(node_local, settings, probe, client):
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = True
    probe.result = _UNREACHABLE_PROBE
    before = _persistent_rows()

    stdout, _stderr, error = _run_expecting_failure("--mode", "release", "--format", "json")

    checks = {c["code"]: c for c in _json_report(stdout)["checks"]}
    assert checks["CENTRAL_CONNECTIVITY"]["status"] == "FAIL"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"]["status"] == "FAIL"
    assert error.returncode == 1
    with pytest.raises(ReleaseDiagnosticsError):
        _validate_release_stdout(stdout, error.returncode)
    with pytest.raises(ReleaseDiagnosticsError):
        _validate_release_stdout(stdout, 0)  # overall FAIL fails closed regardless
    assert _persistent_rows() == before
    responses = [client.get(path) for path in ("/ready/", "/readyz")]
    assert [r.status_code for r in responses] == [200, 200]


def test_node_release_policy_true_rejecting_central_fails_auth(node_local, settings, probe):
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = True
    probe.result = _REJECTED_PROBE
    stdout, _stderr, error = _run_expecting_failure("--mode", "release", "--format", "json")
    checks = {c["code"]: c for c in _json_report(stdout)["checks"]}
    assert checks["CENTRAL_CONNECTIVITY"]["status"] == "PASS"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"]["status"] == "FAIL"
    assert error.returncode == 1


@pytest.mark.parametrize("policy", ["maybe", CONFIG_SENTINEL])
def test_node_release_malformed_policy_fails_closed(node_local, settings, probe, policy):
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = policy
    stdout, stderr, error = _run_expecting_failure("--mode", "release", "--format", "json")
    checks = {c["code"]: c for c in _json_report(stdout)["checks"]}
    assert checks["SYNC_RETRY_POLICY"]["status"] == "FAIL"
    assert error.returncode == 1
    assert probe.calls == []
    assert CONFIG_SENTINEL not in stdout + stderr + str(error)


@pytest.mark.parametrize("policy", [True, "maybe"])
def test_central_release_ignores_node_policy_and_never_probes(central_local, settings, probe, policy):
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = policy

    stdout, _ = _run("--mode", "release", "--format", "json")

    report = _json_report(stdout)
    checks = {c["code"]: c for c in report["checks"]}
    assert report["profile"] == "central"
    assert report["mode"] == "release"
    assert set(checks) == {"DB_CONNECTIVITY", "SCHEMA_STATE", "SIGNING_KEY", "NODE_REGISTRATION"}
    assert probe.calls == []
    assert _validate_release_stdout(stdout).overall_status == "PASS"


def test_release_text_renders_the_same_report(node_local, settings, probe):
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = True
    json_stdout, _ = _run("--mode", "release", "--format", "json")
    text_stdout, _ = _run("--mode", "release", "--format", "text")
    report = _json_report(json_stdout)
    header, text_checks = _parse_text(text_stdout)
    assert header["mode"] == "release"
    assert text_checks == {c["code"]: c for c in report["checks"]}
    assert len(probe.calls) == 2


@pytest.mark.parametrize(
    "argv",
    [
        ("--mode", "release", "--require-central-probe", "false"),
        ("--mode", "release", "--no-remote"),
        ("--mode", "release", "--skip-check", "CENTRAL_CONNECTIVITY"),
        ("--mode", "release", "--force"),
    ],
)
def test_no_cli_switch_can_override_release_policy(node_local, settings, probe, argv):
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = True
    probe.result = _UNREACHABLE_PROBE
    out = io.StringIO()
    with pytest.raises((CommandError, SystemExit)):
        call_command("diagnose_deployment", *argv, stdout=out)
    assert out.getvalue() == ""
    assert probe.calls == []


def test_release_sentinels_never_leak(node_local, settings, probe, caplog, tmp_path):
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = True
    settings.LINUXLAB_CENTRAL_URL = f"https://central.example.org/#{AUTHORIZATION_SENTINEL}"
    stdout, stderr, error = _run_expecting_failure("--mode", "release", "--format", "json")
    everything = stdout + stderr + str(error) + repr(error) + caplog.text
    for sentinel in (NODE_SECRET_SENTINEL, "AUTHZ_SENTINEL", "Bearer", str(tmp_path)):
        assert sentinel not in everything
    assert probe.calls == []
