"""Corrective B Task 10: SyncDiagnosticProvider owns the LOCAL checks
TRUST_KEY, NODE_REGISTRATION and SYNC_RETRY_POLICY (approved spec §§4-6).
Private per-instance settings snapshot; Central registration inventory
semantics; Node local credential evidence is WARN/authority_verified=false;
typed configuration authority; zero network; zero mutation."""

import uuid
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from linuxlab.core.diagnostics import DiagnosticContext, aggregate_diagnostics, report_to_json
from linuxlab.sync.authentication import (
    issue_node_registration,
    revoke_node_registration,
    rotate_node_credential,
)
from linuxlab.sync.configuration import DeferredSyncSettingError
from linuxlab.sync.diagnostics import SyncDiagnosticProvider
from linuxlab.sync.models import (
    REGISTRATION_ACTIVE,
    NodeRegistration,
    OutboxMessage,
    _insert_immutable_row,
)

pytestmark = pytest.mark.django_db

SECRET_SENTINEL = "SYNC_SECRET_SENTINEL_4d2e"
CONFIG_SENTINEL = "CONFIG_VALUE_SENTINEL_b81f"

NODE_FULL = DiagnosticContext(
    "node",
    "local",
    frozenset(
        {
            "LINUXLAB_CAN_IMPORT_PACKAGES",
            "LINUXLAB_CAN_SERVE_LEARNER_CONTENT",
            "LINUXLAB_CAN_RUN_SYNC_WORKER",
        }
    ),
)
NODE_NO_IMPORT = DiagnosticContext(
    "node", "local", frozenset({"LINUXLAB_CAN_SERVE_LEARNER_CONTENT", "LINUXLAB_CAN_RUN_SYNC_WORKER"})
)
CENTRAL_FULL = DiagnosticContext(
    "central", "local", frozenset({"LINUXLAB_CAN_AUTHOR", "LINUXLAB_CAN_RECEIVE_SYNC_EVENTS"})
)
CENTRAL_NO_RECEIVE = DiagnosticContext("central", "local", frozenset({"LINUXLAB_CAN_AUTHOR"}))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("LOCAL diagnostics must never call Central")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", _forbidden)
    monkeypatch.setattr("urllib.request.urlopen", _forbidden)
    monkeypatch.setattr("linuxlab.sync.transports._TRANSPORT_OPENER.open", _forbidden)


@pytest.fixture
def trust_key(monkeypatch, tmp_path):
    key_path = tmp_path / "package-signing-public.key"
    public_key = Ed25519PrivateKey.generate().public_key()
    key_path.write_bytes(public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
    return key_path


def _configure_node(settings, **overrides):
    values = {
        "LINUXLAB_PROFILE": "node",
        "LINUXLAB_CAN_RECEIVE_SYNC_EVENTS": False,
        "LINUXLAB_CAN_RUN_SYNC_WORKER": True,
        "LINUXLAB_SYNC_ENABLED": True,
        "LINUXLAB_NODE_ID": str(uuid.uuid4()),
        "LINUXLAB_SYNC_SECRET": SECRET_SENTINEL,
        "LINUXLAB_CENTRAL_URL": "https://central.example.org",
        "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE": False,
    }
    values.update(overrides)
    for name, value in values.items():
        setattr(settings, name, value)


def _configure_central(settings, **overrides):
    values = {
        "LINUXLAB_PROFILE": "central",
        "LINUXLAB_CAN_RECEIVE_SYNC_EVENTS": True,
        "LINUXLAB_CAN_RUN_SYNC_WORKER": False,
        "LINUXLAB_SYNC_ENABLED": False,
        "LINUXLAB_NODE_ID": None,
        "LINUXLAB_SYNC_SECRET": None,
        "LINUXLAB_CENTRAL_URL": None,
    }
    values.update(overrides)
    for name, value in values.items():
        setattr(settings, name, value)


def _checks(provider, context):
    checks = provider.checks(context)
    return {check.code: check for check in checks}


def _rendered(context, checks):
    report = aggregate_diagnostics(context, checks)
    return report_to_json(report) + repr(report) + " ".join(repr(c) for c in checks)


def _registration_rows():
    return list(NodeRegistration.objects.order_by("pk").values())


# --- ownership / LOCAL selection matrix ------------------------------------


def test_provider_owns_exactly_the_three_local_sync_codes():
    assert SyncDiagnosticProvider.codes == frozenset(
        {"TRUST_KEY", "NODE_REGISTRATION", "SYNC_RETRY_POLICY"}
    )


def test_node_with_outbound_sync_enabled_requires_registration_policy_and_trust(settings, trust_key):
    _configure_node(settings)
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(NODE_FULL) == frozenset(
        {"TRUST_KEY", "NODE_REGISTRATION", "SYNC_RETRY_POLICY"}
    )
    assert provider.required_codes(NODE_NO_IMPORT) == frozenset(
        {"NODE_REGISTRATION", "SYNC_RETRY_POLICY"}
    )


def test_node_with_sync_disabled_requires_only_trust_when_importing(settings, trust_key):
    _configure_node(settings, LINUXLAB_SYNC_ENABLED=False)
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(NODE_FULL) == frozenset({"TRUST_KEY"})
    assert provider.required_codes(NODE_NO_IMPORT) == frozenset()
    assert set(_checks(provider, NODE_FULL)) == {"TRUST_KEY"}
    assert _checks(provider, NODE_NO_IMPORT) == {}


def test_central_requires_registration_only_when_receiving_sync(settings):
    _configure_central(settings)
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(CENTRAL_FULL) == frozenset({"NODE_REGISTRATION"})
    assert provider.required_codes(CENTRAL_NO_RECEIVE) == frozenset()
    assert set(_checks(provider, CENTRAL_FULL)) == {"NODE_REGISTRATION"}
    assert _checks(provider, CENTRAL_NO_RECEIVE) == {}


def test_central_never_applies_node_sync_policy_even_when_malformed(settings):
    _configure_central(
        settings,
        LINUXLAB_SYNC_ENABLED=CONFIG_SENTINEL,
        LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=CONFIG_SENTINEL,
        LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS=CONFIG_SENTINEL,
    )
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(CENTRAL_FULL) == frozenset({"NODE_REGISTRATION"})
    checks = _checks(provider, CENTRAL_FULL)
    assert set(checks) == {"NODE_REGISTRATION"}
    assert CONFIG_SENTINEL not in _rendered(CENTRAL_FULL, checks.values())


# --- private snapshot --------------------------------------------------------


def test_required_codes_and_checks_use_the_same_construction_time_snapshot(settings, trust_key):
    _configure_node(settings)
    provider = SyncDiagnosticProvider()
    required_before = provider.required_codes(NODE_FULL)

    settings.LINUXLAB_SYNC_ENABLED = False
    settings.LINUXLAB_NODE_ID = "not-a-uuid"

    assert provider.required_codes(NODE_FULL) == required_before
    checks = _checks(provider, NODE_FULL)
    assert checks["NODE_REGISTRATION"].status == "WARN"
    fresh = SyncDiagnosticProvider()
    assert fresh.required_codes(NODE_FULL) == frozenset({"TRUST_KEY"})


def test_provider_construction_reads_no_db_no_keys_no_network(settings, django_assert_num_queries):
    _configure_node(settings)
    with (
        django_assert_num_queries(0),
        patch("linuxlab.sync.trust.load_package_trusted_public_key") as trust,
    ):
        SyncDiagnosticProvider()
    trust.assert_not_called()


# --- Node NODE_REGISTRATION: local evidence only ----------------------------


def test_node_valid_local_credentials_are_warn_and_unverified(settings, trust_key):
    _configure_node(settings)
    checks = _checks(SyncDiagnosticProvider(), NODE_FULL)
    check = checks["NODE_REGISTRATION"]
    assert check.status == "WARN"
    assert check.details["authority_verified"] is False
    assert "PASS" not in check.message
    assert "active" not in check.message.lower()
    assert SECRET_SENTINEL not in _rendered(NODE_FULL, checks.values())


@pytest.mark.parametrize(
    "name,value",
    [
        ("LINUXLAB_NODE_ID", "not-a-uuid"),
        ("LINUXLAB_NODE_ID", None),
        ("LINUXLAB_NODE_ID", CONFIG_SENTINEL),
        ("LINUXLAB_SYNC_SECRET", ""),
        ("LINUXLAB_SYNC_SECRET", "   "),
        ("LINUXLAB_SYNC_SECRET", None),
    ],
)
def test_node_malformed_identity_or_blank_secret_fails(settings, trust_key, name, value):
    _configure_node(settings, **{name: value})
    checks = _checks(SyncDiagnosticProvider(), NODE_FULL)
    check = checks["NODE_REGISTRATION"]
    assert check.status == "FAIL"
    assert check.details["authority_verified"] is False
    assert check.details["error_code"] == "NODE_CREDENTIALS_INVALID"
    assert CONFIG_SENTINEL not in _rendered(NODE_FULL, checks.values())


def test_node_never_reads_central_registration_table(settings, trust_key):
    _configure_node(settings)
    with patch("linuxlab.sync.diagnostics.NodeRegistration") as table:
        checks = _checks(SyncDiagnosticProvider(), NODE_FULL)
    assert checks["NODE_REGISTRATION"].status == "WARN"
    assert not table.objects.mock_calls


# --- SYNC_RETRY_POLICY: typed configuration authority -----------------------


def test_node_valid_policy_and_prerequisites_pass(settings, trust_key):
    _configure_node(settings)
    checks = _checks(SyncDiagnosticProvider(), NODE_FULL)
    assert checks["SYNC_RETRY_POLICY"].status == "PASS"


@pytest.mark.parametrize(
    "name,value",
    [
        ("LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS", CONFIG_SENTINEL),
        ("LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS", 0),
        ("LINUXLAB_SYNC_BACKOFF_BASE_SECONDS", 9999.0),  # base >= cap
        ("LINUXLAB_SYNC_RETRY_MAX_AGE", CONFIG_SENTINEL),
        ("LINUXLAB_SYNC_BATCH_MAX_BYTES", CONFIG_SENTINEL),
        ("LINUXLAB_SYNC_LEASE_SECONDS", 0),
        ("LINUXLAB_CAN_RUN_SYNC_WORKER", False),
    ],
)
def test_node_malformed_policy_or_prerequisite_fails_retry_policy(settings, trust_key, name, value):
    _configure_node(settings, **{name: value})
    checks = _checks(SyncDiagnosticProvider(), NODE_FULL)
    assert checks["SYNC_RETRY_POLICY"].status == "FAIL"
    assert checks["SYNC_RETRY_POLICY"].details["error_code"].startswith("SYNC_CONFIG_")
    assert CONFIG_SENTINEL not in _rendered(NODE_FULL, checks.values())


@pytest.mark.parametrize(
    "url",
    [CONFIG_SENTINEL, "http://central.example.org", "https://user:pw@central.example.org", None],
)
def test_invalid_central_url_belongs_to_retry_policy_not_registration(settings, trust_key, url):
    _configure_node(settings, LINUXLAB_CENTRAL_URL=url)
    checks = _checks(SyncDiagnosticProvider(), NODE_FULL)
    assert checks["SYNC_RETRY_POLICY"].status == "FAIL"
    assert checks["NODE_REGISTRATION"].status == "WARN"
    assert CONFIG_SENTINEL not in _rendered(NODE_FULL, checks.values())


@pytest.mark.parametrize("value", ["maybe", CONFIG_SENTINEL, 1, "", None])
def test_malformed_require_probe_fails_retry_policy_never_false(settings, trust_key, value):
    _configure_node(settings, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=value)
    checks = _checks(SyncDiagnosticProvider(), NODE_FULL)
    assert checks["SYNC_RETRY_POLICY"].status == "FAIL"
    assert CONFIG_SENTINEL not in _rendered(NODE_FULL, checks.values())


@pytest.mark.parametrize(
    "value",
    [
        CONFIG_SENTINEL,
        "",
        1,
        DeferredSyncSettingError("LINUXLAB_SYNC_ENABLED", "INVALID_VALUE"),
    ],
)
def test_malformed_or_deferred_enabled_is_a_configuration_fail_not_truthiness(
    settings, trust_key, value
):
    _configure_node(settings, LINUXLAB_SYNC_ENABLED=value)
    provider = SyncDiagnosticProvider()
    assert "SYNC_RETRY_POLICY" in provider.required_codes(NODE_FULL)
    checks = _checks(provider, NODE_FULL)
    assert checks["SYNC_RETRY_POLICY"].status == "FAIL"
    assert CONFIG_SENTINEL not in _rendered(NODE_FULL, checks.values())


def test_retry_policy_check_performs_no_claim_flush_or_requeue(settings, trust_key):
    _configure_node(settings)
    with (
        patch("linuxlab.sync.outbox.claim_outbox_batch") as claim,
        patch("linuxlab.sync.outbox.flush_outbox") as flush,
        patch("linuxlab.sync.outbox.requeue_outbox_message") as requeue,
    ):
        _checks(SyncDiagnosticProvider(), NODE_FULL)
    claim.assert_not_called()
    flush.assert_not_called()
    requeue.assert_not_called()
    assert OutboxMessage.objects.count() == 0


# --- TRUST_KEY ---------------------------------------------------------------


def test_valid_trust_key_passes_and_is_unchanged(settings, trust_key):
    _configure_node(settings)
    before = trust_key.read_bytes()
    checks = _checks(SyncDiagnosticProvider(), NODE_FULL)
    assert checks["TRUST_KEY"].status == "PASS"
    assert trust_key.read_bytes() == before


@pytest.mark.parametrize("content", [b"", b"TRUST_PEM_SENTINEL garbage"])
def test_malformed_trust_key_fails_safely(settings, monkeypatch, tmp_path, content):
    _configure_node(settings)
    key_path = tmp_path / "package-signing-public.key"
    key_path.write_bytes(content)
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
    checks = _checks(SyncDiagnosticProvider(), NODE_FULL)
    assert checks["TRUST_KEY"].status == "FAIL"
    assert checks["TRUST_KEY"].details["error_code"] == "TRUST_KEY_INVALID"
    rendered = _rendered(NODE_FULL, checks.values())
    assert "TRUST_PEM_SENTINEL" not in rendered
    assert str(tmp_path) not in rendered


def test_missing_trust_key_fails_safely(settings, monkeypatch, tmp_path):
    _configure_node(settings)
    monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "missing.key")
    checks = _checks(SyncDiagnosticProvider(), NODE_FULL)
    assert checks["TRUST_KEY"].status == "FAIL"
    assert str(tmp_path) not in _rendered(NODE_FULL, checks.values())


# --- Central NODE_REGISTRATION inventory ------------------------------------


def test_central_empty_inventory_passes_with_zero_count(settings):
    _configure_central(settings)
    checks = _checks(SyncDiagnosticProvider(), CENTRAL_FULL)
    check = checks["NODE_REGISTRATION"]
    assert check.status == "PASS"
    assert check.details["node_count"] == 0
    assert check.details["invalid_count"] == 0


def test_central_active_with_history_passes_without_warning(settings):
    _configure_central(settings)
    credential = issue_node_registration("Lab A")
    rotate_node_credential(credential.node_id)
    other = issue_node_registration("Lab B")
    revoke_node_registration(other.node_id)
    replacement = issue_node_registration("Lab B again", node_id=other.node_id)
    before = _registration_rows()

    checks = _checks(SyncDiagnosticProvider(), CENTRAL_FULL)

    check = checks["NODE_REGISTRATION"]
    assert check.status == "PASS"
    assert check.details["node_count"] == 2
    assert check.details["invalid_count"] == 0
    assert _registration_rows() == before
    rendered = _rendered(CENTRAL_FULL, checks.values())
    assert credential.secret not in rendered
    assert replacement.secret not in rendered


def test_central_history_only_logical_node_fails(settings):
    _configure_central(settings)
    node_id = uuid.uuid4()
    _insert_immutable_row(
        NodeRegistration,
        node_id=node_id,
        display_name="History only",
        state="superseded",
        current_secret_hash="!unusable",
        previous_secret_hash="",
        previous_valid_until=None,
        enabled=True,
    )
    checks = _checks(SyncDiagnosticProvider(), CENTRAL_FULL)
    check = checks["NODE_REGISTRATION"]
    assert check.status == "FAIL"
    assert check.details["node_count"] == 1
    assert check.details["invalid_count"] == 1
    assert check.details["error_code"] == "REGISTRATION_AUTHORITY_INVALID"


def test_central_revoked_without_replacement_fails(settings):
    _configure_central(settings)
    credential = issue_node_registration("Revoked lab")
    revoke_node_registration(credential.node_id)
    healthy = issue_node_registration("Healthy lab")

    checks = _checks(SyncDiagnosticProvider(), CENTRAL_FULL)

    check = checks["NODE_REGISTRATION"]
    assert check.status == "FAIL"
    assert check.details["node_count"] == 2
    assert check.details["invalid_count"] == 1
    assert healthy.secret not in _rendered(CENTRAL_FULL, checks.values())


def test_central_unusable_active_hash_fails_without_exposing_hash(settings):
    _configure_central(settings)
    _insert_immutable_row(
        NodeRegistration,
        node_id=uuid.uuid4(),
        display_name="Corrupt",
        state=REGISTRATION_ACTIVE,
        current_secret_hash="!HASH_SENTINEL_unusable",
        previous_secret_hash="",
        previous_valid_until=None,
        enabled=True,
    )
    checks = _checks(SyncDiagnosticProvider(), CENTRAL_FULL)
    check = checks["NODE_REGISTRATION"]
    assert check.status == "FAIL"
    assert check.details["invalid_count"] == 1
    rendered = _rendered(CENTRAL_FULL, checks.values())
    assert "HASH_SENTINEL" not in rendered
    assert "Corrupt" not in rendered


def test_central_inventory_details_are_bounded_counts_only(settings):
    _configure_central(settings)
    for index in range(5):
        issue_node_registration(f"Lab {index}")
    checks = _checks(SyncDiagnosticProvider(), CENTRAL_FULL)
    check = checks["NODE_REGISTRATION"]
    assert set(check.details) <= {"node_count", "invalid_count", "error_code"}
    assert check.details["node_count"] == 5
    registrations = NodeRegistration.objects.values_list("pk", "node_id")
    rendered = _rendered(CENTRAL_FULL, checks.values())
    for pk, node_id in registrations:
        assert str(node_id) not in rendered
        assert f" {pk} " not in rendered


def test_central_registration_check_never_mutates_or_provisions(settings):
    _configure_central(settings)
    issue_node_registration("Lab")
    before = _registration_rows()
    with (
        patch("linuxlab.sync.authentication.issue_node_registration") as issue,
        patch("linuxlab.sync.authentication.rotate_node_credential") as rotate,
        patch("linuxlab.sync.authentication.revoke_node_registration") as revoke,
    ):
        _checks(SyncDiagnosticProvider(), CENTRAL_FULL)
    issue.assert_not_called()
    rotate.assert_not_called()
    revoke.assert_not_called()
    assert _registration_rows() == before
