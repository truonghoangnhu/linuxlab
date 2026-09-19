"""Corrective B Task 10: SyncDiagnosticProvider owns the LOCAL checks
TRUST_KEY, NODE_REGISTRATION and SYNC_RETRY_POLICY (approved spec §§4-6).
Private per-instance settings snapshot; Central registration inventory
semantics; Node local credential evidence is WARN/authority_verified=false;
typed configuration authority; zero network; zero mutation."""

import urllib.request
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
from linuxlab.sync.probe import CentralProbeResult
from linuxlab.sync.transports import _TRANSPORT_OPENER

_ORIGINAL_OPENER_OPEN = urllib.request.OpenerDirector.open

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
    """Zero-network poison via the CLASS method only: _TRANSPORT_OPENER
    resolves `.open` through OpenerDirector, so no instance-level patch is
    layered on top (Fix Round 1, T10-I2 -- that layering restored a
    poisoned bound method as an instance shadow attribute after teardown)."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("LOCAL diagnostics must never call Central")

    assert "open" not in vars(_TRANSPORT_OPENER)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", _forbidden)
    monkeypatch.setattr("urllib.request.urlopen", _forbidden)


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


def test_provider_owns_the_three_local_sync_codes_and_the_two_remote_codes():
    # Task 10 ownership (three LOCAL codes) extended by Task 11 with the
    # two REMOTE codes; core's and publication's codes are never owned here.
    assert SyncDiagnosticProvider.codes == frozenset(
        {
            "TRUST_KEY",
            "NODE_REGISTRATION",
            "SYNC_RETRY_POLICY",
            "CENTRAL_CONNECTIVITY",
            "CENTRAL_AUTH_ACCEPTANCE",
        }
    )
    assert not SyncDiagnosticProvider.codes & {"DB_CONNECTIVITY", "SCHEMA_STATE", "SIGNING_KEY"}


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


@pytest.mark.parametrize(
    "value",
    ["maybe", CONFIG_SENTINEL, DeferredSyncSettingError(
        "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE", "INVALID_VALUE"
    )],
)
def test_disabled_node_with_malformed_require_probe_still_fails_retry_policy(
    settings, trust_key, value
):
    # Fix Round 1 (T10-I1): a malformed Node release-probe policy is itself
    # invalid configuration and selects SYNC_RETRY_POLICY even while
    # outbound sync is typed False -- it can never silently disappear (or
    # become false). NODE_REGISTRATION is NOT inferred from it.
    _configure_node(
        settings,
        LINUXLAB_SYNC_ENABLED=False,
        LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=value,
        # Irrelevant disabled-worker prerequisites deliberately absent: the
        # failure must come from the typed policy error itself.
        LINUXLAB_CAN_RUN_SYNC_WORKER=False,
        LINUXLAB_SYNC_SECRET=None,
        LINUXLAB_CENTRAL_URL=None,
    )
    provider = SyncDiagnosticProvider()

    required = provider.required_codes(NODE_FULL)

    assert "SYNC_RETRY_POLICY" in required
    assert "NODE_REGISTRATION" not in required
    checks = _checks(provider, NODE_FULL)
    check = checks["SYNC_RETRY_POLICY"]
    assert check.status == "FAIL"
    assert check.details["error_code"] == "SYNC_CONFIG_INVALID_VALUE"
    assert "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE" in check.message
    rendered = _rendered(NODE_FULL, checks.values())
    assert CONFIG_SENTINEL not in rendered
    assert "maybe" not in rendered
    assert "NODE_REGISTRATION" not in checks


def test_disabled_node_with_valid_require_probe_omits_retry_policy(settings, trust_key):
    _configure_node(
        settings, LINUXLAB_SYNC_ENABLED=False, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=True
    )
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(NODE_FULL) == frozenset({"TRUST_KEY"})
    assert set(_checks(provider, NODE_FULL)) == {"TRUST_KEY"}


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


# --- Fix Round 1 (T10-I2): teardown proof, runs last in file order ---------


def test_network_poison_is_effective_and_leaves_no_instance_shadow():
    # Inside this test the autouse poison is active through the class...
    with pytest.raises(AssertionError, match="never call Central"):
        _TRANSPORT_OPENER.open("https://central.example.org/api/v1/sync/events/")
    # ...and no earlier test in this module left an instance shadow behind.
    assert "open" not in vars(_TRANSPORT_OPENER)
    assert urllib.request.OpenerDirector.open is not _ORIGINAL_OPENER_OPEN  # patched now


# --- Corrective B Task 11: REMOTE mode (Node only) ---------------------------


NODE_REMOTE = DiagnosticContext(
    "node",
    "remote",
    frozenset(
        {
            "LINUXLAB_CAN_IMPORT_PACKAGES",
            "LINUXLAB_CAN_SERVE_LEARNER_CONTENT",
            "LINUXLAB_CAN_RUN_SYNC_WORKER",
        }
    ),
)
CENTRAL_REMOTE = DiagnosticContext(
    "central", "remote", frozenset({"LINUXLAB_CAN_AUTHOR", "LINUXLAB_CAN_RECEIVE_SYNC_EVENTS"})
)
_REMOTE_CODES = frozenset({"CENTRAL_CONNECTIVITY", "CENTRAL_AUTH_ACCEPTANCE"})
_ACCEPTED = CentralProbeResult(reachable=True, accepted=True, http_status=200)
_REJECTED = CentralProbeResult(
    reachable=True, accepted=False, http_status=401, error_code="AUTH_REJECTED"
)
_UNREACHABLE = CentralProbeResult(reachable=False, accepted=False, error_code="PROBE_UNREACHABLE")
_MALFORMED = CentralProbeResult(
    reachable=True, accepted=False, http_status=200, error_code="PROBE_RESPONSE_INVALID"
)


class _ProbeSpy:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, credentials, *, opener=None):
        self.calls.append((credentials, opener))
        return self.result


@pytest.fixture
def probe(monkeypatch):
    spy = _ProbeSpy(_ACCEPTED)
    monkeypatch.setattr("linuxlab.sync.diagnostics.probe_central", spy)
    return spy


def test_provider_now_owns_the_two_remote_codes():
    assert _REMOTE_CODES <= SyncDiagnosticProvider.codes
    assert SyncDiagnosticProvider.codes == frozenset(
        {"TRUST_KEY", "NODE_REGISTRATION", "SYNC_RETRY_POLICY", *_REMOTE_CODES}
    )


def test_node_remote_requires_both_remote_codes_plus_local_registration_and_policy(
    settings, trust_key, probe
):
    _configure_node(settings)
    assert SyncDiagnosticProvider().required_codes(NODE_REMOTE) == frozenset(
        {"TRUST_KEY", "NODE_REGISTRATION", "SYNC_RETRY_POLICY", *_REMOTE_CODES}
    )


def test_node_remote_with_outbound_sync_disabled_still_requires_probe_checks(
    settings, trust_key, probe
):
    # Spec §5: NODE_REGISTRATION / SYNC_RETRY_POLICY are required when a
    # remote probe is requested, even if outbound sync is disabled.
    _configure_node(settings, LINUXLAB_SYNC_ENABLED=False)
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(NODE_REMOTE) == frozenset(
        {"TRUST_KEY", "NODE_REGISTRATION", "SYNC_RETRY_POLICY", *_REMOTE_CODES}
    )
    checks = _checks(provider, NODE_REMOTE)
    assert checks["SYNC_RETRY_POLICY"].status == "PASS"
    assert checks["NODE_REGISTRATION"].status == "WARN"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "PASS"


def test_local_mode_never_requires_or_emits_remote_codes(settings, trust_key, probe):
    _configure_node(settings)
    provider = SyncDiagnosticProvider()
    assert not provider.required_codes(NODE_FULL) & _REMOTE_CODES
    assert not set(_checks(provider, NODE_FULL)) & _REMOTE_CODES
    assert probe.calls == []


def test_central_remote_context_never_requires_remote_codes(settings, probe):
    _configure_central(settings)
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(CENTRAL_REMOTE) == frozenset({"NODE_REGISTRATION"})
    assert set(_checks(provider, CENTRAL_REMOTE)) == {"NODE_REGISTRATION"}
    assert probe.calls == []


def test_remote_probe_is_called_exactly_once_and_both_checks_share_its_result(
    settings, trust_key, probe
):
    _configure_node(settings)
    checks = _checks(SyncDiagnosticProvider(), NODE_REMOTE)

    assert len(probe.calls) == 1
    (credentials, opener) = probe.calls[0]
    assert opener is None
    assert str(credentials.node_id) == settings.LINUXLAB_NODE_ID
    assert credentials.secret == SECRET_SENTINEL
    assert credentials.central_url == settings.LINUXLAB_CENTRAL_URL
    assert checks["CENTRAL_CONNECTIVITY"].status == "PASS"
    assert checks["CENTRAL_CONNECTIVITY"].details["http_status"] == 200
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "PASS"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].details["http_status"] == 200
    assert SECRET_SENTINEL not in _rendered(NODE_REMOTE, checks.values())


def test_remote_auth_success_keeps_local_registration_warn(settings, trust_key, probe):
    _configure_node(settings)
    checks = _checks(SyncDiagnosticProvider(), NODE_REMOTE)
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "PASS"
    assert checks["NODE_REGISTRATION"].status == "WARN"
    assert checks["NODE_REGISTRATION"].details["authority_verified"] is False


@pytest.mark.parametrize("status", [401, 403])
def test_remote_auth_rejection_is_connectivity_pass_auth_fail_without_inference(
    settings, trust_key, probe, status
):
    _configure_node(settings)
    probe.result = CentralProbeResult(
        reachable=True, accepted=False, http_status=status, error_code="AUTH_REJECTED"
    )
    checks = _checks(SyncDiagnosticProvider(), NODE_REMOTE)
    assert checks["CENTRAL_CONNECTIVITY"].status == "PASS"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "FAIL"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].details == {
        "error_code": "AUTH_REJECTED",
        "http_status": status,
    }
    message = checks["CENTRAL_AUTH_ACCEPTANCE"].message.lower()
    for word in ("revoked", "unknown", "superseded", "registration exists"):
        assert word not in message
    assert len(probe.calls) == 1


def test_remote_unreachable_fails_both_checks(settings, trust_key, probe):
    _configure_node(settings)
    probe.result = _UNREACHABLE
    checks = _checks(SyncDiagnosticProvider(), NODE_REMOTE)
    assert checks["CENTRAL_CONNECTIVITY"].status == "FAIL"
    assert checks["CENTRAL_CONNECTIVITY"].details == {"error_code": "PROBE_UNREACHABLE"}
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "FAIL"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].details == {"error_code": "PROBE_UNREACHABLE"}
    assert len(probe.calls) == 1


def test_remote_malformed_success_is_connectivity_pass_auth_fail(settings, trust_key, probe):
    _configure_node(settings)
    probe.result = _MALFORMED
    checks = _checks(SyncDiagnosticProvider(), NODE_REMOTE)
    assert checks["CENTRAL_CONNECTIVITY"].status == "PASS"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "FAIL"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].details["error_code"] == "PROBE_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "name,value",
    [
        ("LINUXLAB_NODE_ID", None),
        ("LINUXLAB_NODE_ID", "not-a-uuid"),
        ("LINUXLAB_SYNC_SECRET", ""),
        ("LINUXLAB_CENTRAL_URL", None),
        ("LINUXLAB_CENTRAL_URL", CONFIG_SENTINEL),
        ("LINUXLAB_CENTRAL_URL", "http://central.example.org"),
        ("LINUXLAB_CENTRAL_URL", "https://central.example.org/health/?deployment=lab"),
        ("LINUXLAB_CAN_RUN_SYNC_WORKER", False),
    ],
)
def test_remote_missing_or_malformed_probe_config_fails_both_without_network(
    settings, trust_key, probe, name, value
):
    _configure_node(settings, LINUXLAB_SYNC_ENABLED=False, **{name: value})
    checks = _checks(SyncDiagnosticProvider(), NODE_REMOTE)
    assert checks["CENTRAL_CONNECTIVITY"].status == "FAIL"
    assert checks["CENTRAL_CONNECTIVITY"].details == {"error_code": "CONFIG_INVALID"}
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "FAIL"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].details == {"error_code": "CONFIG_INVALID"}
    assert probe.calls == []  # and the autouse opener poison proves zero network
    assert CONFIG_SENTINEL not in _rendered(NODE_REMOTE, checks.values())


def test_remote_invalid_probe_config_also_fails_sync_retry_policy_as_owner(
    settings, trust_key, probe
):
    _configure_node(settings, LINUXLAB_SYNC_ENABLED=False, LINUXLAB_CENTRAL_URL=CONFIG_SENTINEL)
    checks = _checks(SyncDiagnosticProvider(), NODE_REMOTE)
    assert checks["SYNC_RETRY_POLICY"].status == "FAIL"
    assert checks["SYNC_RETRY_POLICY"].details["error_code"] == "SYNC_CONFIG_INVALID_VALUE"
    assert "LINUXLAB_CENTRAL_URL" in checks["SYNC_RETRY_POLICY"].message
    assert CONFIG_SENTINEL not in _rendered(NODE_REMOTE, checks.values())


def test_remote_probe_uses_construction_time_credentials_snapshot(settings, trust_key, probe):
    _configure_node(settings)
    provider = SyncDiagnosticProvider()
    settings.LINUXLAB_SYNC_SECRET = "changed-after-construction"
    settings.LINUXLAB_NODE_ID = "not-a-uuid"

    _checks(provider, NODE_REMOTE)

    assert probe.calls[0][0].secret == SECRET_SENTINEL


def test_remote_diagnosis_makes_no_client_side_mutation(settings, trust_key, probe):
    _configure_node(settings)
    from linuxlab.sync.tests.test_retry_policy import _make_outbox as _make_in_flight

    row = _make_in_flight(attempts=1)
    before_outbox = list(OutboxMessage.objects.order_by("pk").values())
    before_registrations = _registration_rows()
    with (
        patch("linuxlab.sync.outbox.claim_outbox_batch") as claim,
        patch("linuxlab.sync.outbox.flush_outbox") as flush,
        patch("linuxlab.sync.outbox.requeue_outbox_message") as requeue,
    ):
        _checks(SyncDiagnosticProvider(), NODE_REMOTE)
    claim.assert_not_called()
    flush.assert_not_called()
    requeue.assert_not_called()
    assert list(OutboxMessage.objects.order_by("pk").values()) == before_outbox
    assert _registration_rows() == before_registrations
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert (refreshed.attempts, refreshed.next_attempt_at, refreshed.first_failure_at) == (
        row.attempts,
        row.next_attempt_at,
        row.first_failure_at,
    )


# --- Corrective B Task 12: RELEASE mode (canonical policy decides) -----------

NODE_RELEASE = DiagnosticContext(
    "node",
    "release",
    frozenset(
        {
            "LINUXLAB_CAN_IMPORT_PACKAGES",
            "LINUXLAB_CAN_SERVE_LEARNER_CONTENT",
            "LINUXLAB_CAN_RUN_SYNC_WORKER",
        }
    ),
)
CENTRAL_RELEASE = DiagnosticContext(
    "central", "release", frozenset({"LINUXLAB_CAN_AUTHOR", "LINUXLAB_CAN_RECEIVE_SYNC_EVENTS"})
)
_LOCAL_SYNC_CODES = frozenset({"NODE_REGISTRATION", "SYNC_RETRY_POLICY"})


def test_release_policy_false_with_sync_disabled_requires_only_local_applicable(
    settings, trust_key, probe
):
    _configure_node(
        settings,
        LINUXLAB_SYNC_ENABLED=False,
        LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=False,
        LINUXLAB_SYNC_SECRET=None,
        LINUXLAB_CENTRAL_URL=None,
    )
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(NODE_RELEASE) == frozenset({"TRUST_KEY"})
    assert set(_checks(provider, NODE_RELEASE)) == {"TRUST_KEY"}
    assert probe.calls == []


def test_release_policy_false_with_sync_enabled_requires_local_sync_checks_only(
    settings, trust_key, probe
):
    _configure_node(settings, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=False)
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(NODE_RELEASE) == frozenset({"TRUST_KEY", *_LOCAL_SYNC_CODES})
    checks = _checks(provider, NODE_RELEASE)
    assert not set(checks) & _REMOTE_CODES
    assert checks["NODE_REGISTRATION"].status == "WARN"
    assert checks["SYNC_RETRY_POLICY"].status == "PASS"
    assert probe.calls == []


@pytest.mark.parametrize("enabled", [True, False])
def test_release_policy_true_requires_both_remote_checks_and_prerequisites(
    settings, trust_key, probe, enabled
):
    _configure_node(
        settings, LINUXLAB_SYNC_ENABLED=enabled, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=True
    )
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(NODE_RELEASE) == frozenset(
        {"TRUST_KEY", *_LOCAL_SYNC_CODES, *_REMOTE_CODES}
    )
    checks = _checks(provider, NODE_RELEASE)
    assert len(probe.calls) == 1
    assert checks["CENTRAL_CONNECTIVITY"].status == "PASS"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "PASS"
    assert checks["SYNC_RETRY_POLICY"].status == "PASS"
    assert checks["NODE_REGISTRATION"].status == "WARN"
    assert checks["NODE_REGISTRATION"].details["authority_verified"] is False


def test_release_policy_true_with_offline_central_fails_both_remote_checks(
    settings, trust_key, probe
):
    _configure_node(settings, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=True)
    probe.result = _UNREACHABLE
    checks = _checks(SyncDiagnosticProvider(), NODE_RELEASE)
    assert checks["CENTRAL_CONNECTIVITY"].status == "FAIL"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "FAIL"
    assert len(probe.calls) == 1


def test_release_policy_true_with_rejecting_central_fails_auth_only(settings, trust_key, probe):
    _configure_node(settings, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=True)
    probe.result = _REJECTED
    checks = _checks(SyncDiagnosticProvider(), NODE_RELEASE)
    assert checks["CENTRAL_CONNECTIVITY"].status == "PASS"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "FAIL"
    assert checks["CENTRAL_AUTH_ACCEPTANCE"].details["error_code"] == "AUTH_REJECTED"


@pytest.mark.parametrize(
    "name,value",
    [
        ("LINUXLAB_CENTRAL_URL", None),
        ("LINUXLAB_CENTRAL_URL", CONFIG_SENTINEL),
        ("LINUXLAB_SYNC_SECRET", ""),
        ("LINUXLAB_NODE_ID", "not-a-uuid"),
    ],
)
def test_release_policy_true_with_invalid_prerequisites_fails_closed_without_network(
    settings, trust_key, probe, name, value
):
    _configure_node(
        settings,
        LINUXLAB_SYNC_ENABLED=False,
        LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=True,
        **{name: value},
    )
    provider = SyncDiagnosticProvider()
    required = provider.required_codes(NODE_RELEASE)
    assert "SYNC_RETRY_POLICY" in required
    checks = _checks(provider, NODE_RELEASE)
    assert checks["SYNC_RETRY_POLICY"].status == "FAIL"
    assert checks["SYNC_RETRY_POLICY"].details["error_code"].startswith("SYNC_CONFIG_")
    if _REMOTE_CODES <= required:
        assert checks["CENTRAL_CONNECTIVITY"].details == {"error_code": "CONFIG_INVALID"}
        assert checks["CENTRAL_AUTH_ACCEPTANCE"].details == {"error_code": "CONFIG_INVALID"}
    assert probe.calls == []
    assert CONFIG_SENTINEL not in _rendered(NODE_RELEASE, checks.values())


@pytest.mark.parametrize(
    "value",
    ["maybe", CONFIG_SENTINEL, "", None, 1,
     DeferredSyncSettingError("LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE", "INVALID_VALUE")],
)
def test_release_malformed_policy_fails_closed_never_false(settings, trust_key, probe, value):
    _configure_node(
        settings, LINUXLAB_SYNC_ENABLED=False, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=value
    )
    provider = SyncDiagnosticProvider()
    assert "SYNC_RETRY_POLICY" in provider.required_codes(NODE_RELEASE)
    checks = _checks(provider, NODE_RELEASE)
    assert checks["SYNC_RETRY_POLICY"].status == "FAIL"
    assert probe.calls == []
    assert CONFIG_SENTINEL not in _rendered(NODE_RELEASE, checks.values())


def test_release_malformed_enabled_fails_closed(settings, trust_key, probe):
    _configure_node(settings, LINUXLAB_SYNC_ENABLED=CONFIG_SENTINEL)
    checks = _checks(SyncDiagnosticProvider(), NODE_RELEASE)
    assert checks["SYNC_RETRY_POLICY"].status == "FAIL"
    assert probe.calls == []
    assert CONFIG_SENTINEL not in _rendered(NODE_RELEASE, checks.values())


@pytest.mark.parametrize("policy", [True, "maybe", CONFIG_SENTINEL])
def test_central_release_ignores_node_policy_and_never_probes(settings, probe, policy):
    _configure_central(settings, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=policy)
    provider = SyncDiagnosticProvider()
    assert provider.required_codes(CENTRAL_RELEASE) == frozenset({"NODE_REGISTRATION"})
    checks = _checks(provider, CENTRAL_RELEASE)
    assert set(checks) == {"NODE_REGISTRATION"}
    assert probe.calls == []
    assert CONFIG_SENTINEL not in _rendered(CENTRAL_RELEASE, checks.values())


def test_release_requirements_come_from_the_construction_snapshot(settings, trust_key, probe):
    _configure_node(settings, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=False)
    provider = SyncDiagnosticProvider()
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = True
    assert not provider.required_codes(NODE_RELEASE) & _REMOTE_CODES
    assert SyncDiagnosticProvider().required_codes(NODE_RELEASE) >= _REMOTE_CODES


def test_release_diagnosis_makes_no_client_side_mutation(settings, trust_key, probe):
    _configure_node(settings, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE=True)
    before = list(OutboxMessage.objects.order_by("pk").values()), _registration_rows()
    with (
        patch("linuxlab.sync.outbox.claim_outbox_batch") as claim,
        patch("linuxlab.sync.outbox.flush_outbox") as flush,
        patch("linuxlab.sync.outbox.requeue_outbox_message") as requeue,
    ):
        _checks(SyncDiagnosticProvider(), NODE_RELEASE)
    claim.assert_not_called()
    flush.assert_not_called()
    requeue.assert_not_called()
    assert (list(OutboxMessage.objects.order_by("pk").values()), _registration_rows()) == before
