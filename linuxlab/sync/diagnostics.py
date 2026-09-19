"""Sync's own diagnostic provider (Corrective B Task 10, approved spec
§§4-6): the LOCAL checks TRUST_KEY, NODE_REGISTRATION and
SYNC_RETRY_POLICY; Corrective B Task 11 (spec §7) adds the REMOTE checks
CENTRAL_CONNECTIVITY and CENTRAL_AUTH_ACCEPTANCE, Node-only, derived from
exactly ONE probe_central() call per diagnosis. Task 12 adds RELEASE.

Every relevant setting is read ONCE, into a private per-instance snapshot
at construction (typed loader results and typed deferred failures kept as
they are -- never coerced through truthiness), so required_codes() and
checks() can never disagree. No global cache, no startup snapshot: every
diagnosis constructs a fresh provider through the registry factory.

Central NODE_REGISTRATION inspects Central's OWN registration table with
aggregate counts plus bounded row iteration for structural usability
(Task-5 registration_is_usable()); Node NODE_REGISTRATION inspects only the
locally provisioned identity/secret through load_node_identity() and can
never claim Central authority (WARN, authority_verified=false). LOCAL mode
makes zero Central calls, zero claims/flushes/requeues, zero writes.
"""

from dataclasses import dataclass
from uuid import UUID

from django.conf import settings
from django.db import DatabaseError
from django.db.models import Count

from linuxlab.core.diagnostics import DiagnosticCheck, DiagnosticContext

from .authentication import registration_is_usable
from .configuration import (
    DeferredSyncSettingError,
    SyncConfiguration,
    SyncConfigurationError,
    load_node_identity,
    load_sync_configuration,
    parse_sync_setting,
)
from .models import REGISTRATION_ACTIVE, NodeRegistration
from .probe import (
    AUTH_REJECTED,
    CONFIG_INVALID,
    PROBE_RESPONSE_INVALID,
    PROBE_UNREACHABLE,
    probe_central,
)
from .trust import PackageTrustConfigurationError, load_package_trusted_public_key

_TRUST_KEY_INVALID = {"error_code": "TRUST_KEY_INVALID"}
_NODE_CREDENTIALS_INVALID = {"error_code": "NODE_CREDENTIALS_INVALID", "authority_verified": False}
_NODE_UNVERIFIED = {"authority_verified": False}
_REGISTRATION_INVALID_CODE = "REGISTRATION_AUTHORITY_INVALID"
_DB_UNAVAILABLE = {"error_code": "DB_UNAVAILABLE"}
_ACTIVE_ROW_CHUNK = 200
_REMOTE_CODES = frozenset({"CENTRAL_CONNECTIVITY", "CENTRAL_AUTH_ACCEPTANCE"})
_PROBE_CONFIG_INVALID = {"error_code": CONFIG_INVALID}
_PROBE_UNREACHABLE_DETAILS = {"error_code": PROBE_UNREACHABLE}
_PROBE_FAILURE_CODES = frozenset({AUTH_REJECTED, PROBE_RESPONSE_INVALID})


@dataclass(frozen=True)
class _SyncSettingsSnapshot:
    """Typed values or typed failures, captured once per provider instance."""

    enabled: bool | DeferredSyncSettingError
    require_central_probe: bool | DeferredSyncSettingError
    identity: UUID | SyncConfigurationError
    worker: SyncConfiguration | SyncConfigurationError
    probe: SyncConfiguration | SyncConfigurationError


def _capture_snapshot(source) -> _SyncSettingsSnapshot:
    enabled = parse_sync_setting(
        "LINUXLAB_SYNC_ENABLED", getattr(source, "LINUXLAB_SYNC_ENABLED", False)
    )
    require_probe = parse_sync_setting(
        "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE",
        getattr(source, "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE", False),
    )
    try:
        identity = load_node_identity(settings_obj=source)
    except SyncConfigurationError as exc:
        identity = exc
    try:
        worker = load_sync_configuration(settings_obj=source, purpose="worker")
    except SyncConfigurationError as exc:
        worker = exc
    try:
        probe = load_sync_configuration(settings_obj=source, purpose="probe")
    except SyncConfigurationError as exc:
        probe = exc
    return _SyncSettingsSnapshot(enabled, require_probe, identity, worker, probe)


def _config_failure(setting_name: str, code: str) -> DiagnosticCheck:
    # setting_name/code come only from SyncConfigurationError /
    # DeferredSyncSettingError, whose constructors restrict both to fixed
    # closed vocabularies -- never a setting VALUE.
    return DiagnosticCheck(
        "SYNC_RETRY_POLICY",
        "FAIL",
        f"Sync configuration setting {setting_name} is invalid ({code}).",
        {"error_code": f"SYNC_CONFIG_{code}"},
    )


class SyncDiagnosticProvider:
    codes = frozenset({"TRUST_KEY", "NODE_REGISTRATION", "SYNC_RETRY_POLICY", *_REMOTE_CODES})

    def __init__(self):
        self._snapshot = _capture_snapshot(settings)

    # -- selection -----------------------------------------------------------

    def required_codes(self, context: DiagnosticContext) -> frozenset[str]:
        required = set()
        if "LINUXLAB_CAN_IMPORT_PACKAGES" in context.capabilities:
            required.add("TRUST_KEY")
        if context.profile == "central":
            if "LINUXLAB_CAN_RECEIVE_SYNC_EVENTS" in context.capabilities:
                required.add("NODE_REGISTRATION")
        elif context.profile == "node":
            enabled = self._snapshot.enabled
            remote = context.mode == "remote"
            # Spec §5: NODE_REGISTRATION and SYNC_RETRY_POLICY are required
            # when outbound sync is enabled OR a remote probe is requested.
            if enabled is True or remote:
                required.update({"NODE_REGISTRATION", "SYNC_RETRY_POLICY"})
            elif isinstance(enabled, DeferredSyncSettingError):
                # A malformed/deferred opt-in is a required configuration
                # FAIL -- never truthiness-based enablement or silent false.
                required.add("SYNC_RETRY_POLICY")
            if isinstance(self._snapshot.require_central_probe, DeferredSyncSettingError):
                # Fix Round 1 (T10-I1): a malformed Node release-probe policy
                # is itself invalid configuration and selects
                # SYNC_RETRY_POLICY even while outbound sync is typed False;
                # it can never silently disappear or become false. It does
                # NOT infer outbound sync, so NODE_REGISTRATION stays as
                # decided by the typed enabled value above.
                required.add("SYNC_RETRY_POLICY")
            if remote:
                # Task 11: explicit REMOTE request, Node only. Central
                # never owns the remote checks (the command rejects
                # Central REMOTE before any provider runs).
                required.update(_REMOTE_CODES)
        return frozenset(required)

    def checks(self, context: DiagnosticContext) -> tuple[DiagnosticCheck, ...]:
        required = self.required_codes(context)
        # Task 11: at most ONE probe per diagnosis, shared by both remote
        # checks -- never one call for connectivity and another for auth,
        # and never any call when the remote codes are not required.
        probe_result = self._probe_once() if required & _REMOTE_CODES else None
        handlers = {
            "TRUST_KEY": self._check_trust_key,
            "NODE_REGISTRATION": self._check_node_registration,
            "SYNC_RETRY_POLICY": self._check_sync_retry_policy,
            "CENTRAL_CONNECTIVITY": lambda ctx: self._check_central_connectivity(probe_result),
            "CENTRAL_AUTH_ACCEPTANCE": lambda ctx: self._check_central_auth_acceptance(
                probe_result
            ),
        }
        return tuple(handlers[code](context) for code in sorted(required))

    # -- TRUST_KEY -----------------------------------------------------------

    def _check_trust_key(self, context: DiagnosticContext) -> DiagnosticCheck:
        del context
        try:
            load_package_trusted_public_key()
        except PackageTrustConfigurationError:
            return DiagnosticCheck(
                "TRUST_KEY",
                "FAIL",
                "Node package trust key is missing or is not a valid Ed25519 public key.",
                _TRUST_KEY_INVALID,
            )
        return DiagnosticCheck("TRUST_KEY", "PASS", "Node package trust key is readable and valid.")

    # -- NODE_REGISTRATION ---------------------------------------------------

    def _check_node_registration(self, context: DiagnosticContext) -> DiagnosticCheck:
        if context.profile == "central":
            return self._central_registration_inventory()
        return self._node_local_credentials()

    def _node_local_credentials(self) -> DiagnosticCheck:
        if isinstance(self._snapshot.identity, SyncConfigurationError):
            return DiagnosticCheck(
                "NODE_REGISTRATION",
                "FAIL",
                "Local Node credential configuration is missing or malformed.",
                _NODE_CREDENTIALS_INVALID,
            )
        return DiagnosticCheck(
            "NODE_REGISTRATION",
            "WARN",
            "Local Node credentials are configured; current Central registration "
            "authority is unverified locally.",
            _NODE_UNVERIFIED,
        )

    def _central_registration_inventory(self) -> DiagnosticCheck:
        try:
            node_count = NodeRegistration.objects.values("node_id").distinct().count()
            active_rows = NodeRegistration.objects.filter(state=REGISTRATION_ACTIVE)
            active_node_count = active_rows.values("node_id").distinct().count()
            invalid_active_nodes = set()
            duplicates = (
                active_rows.values("node_id")
                .annotate(active_count=Count("pk"))
                .filter(active_count__gt=1)
            )
            for row in duplicates:
                invalid_active_nodes.add(row["node_id"])
            for registration in active_rows.only("node_id", "current_secret_hash").iterator(
                chunk_size=_ACTIVE_ROW_CHUNK
            ):
                if not registration_is_usable(registration):
                    invalid_active_nodes.add(registration.node_id)
        except DatabaseError:
            return DiagnosticCheck(
                "NODE_REGISTRATION",
                "FAIL",
                "Central registration inventory could not be read.",
                _DB_UNAVAILABLE,
            )
        zero_active_nodes = node_count - active_node_count
        invalid_count = zero_active_nodes + len(invalid_active_nodes)
        if invalid_count:
            return DiagnosticCheck(
                "NODE_REGISTRATION",
                "FAIL",
                "At least one registered logical node lacks exactly one usable active "
                "registration.",
                {
                    "node_count": node_count,
                    "invalid_count": invalid_count,
                    "error_code": _REGISTRATION_INVALID_CODE,
                },
            )
        return DiagnosticCheck(
            "NODE_REGISTRATION",
            "PASS",
            "Every registered logical node has exactly one usable active registration.",
            {"node_count": node_count, "invalid_count": 0},
        )

    # -- SYNC_RETRY_POLICY ---------------------------------------------------

    def _check_sync_retry_policy(self, context: DiagnosticContext) -> DiagnosticCheck:
        snapshot = self._snapshot
        if isinstance(snapshot.enabled, DeferredSyncSettingError):
            return _config_failure(snapshot.enabled.setting_name, snapshot.enabled.code)
        if isinstance(snapshot.require_central_probe, DeferredSyncSettingError):
            # The typed policy error itself -- evaluated before, and
            # independently of, worker prerequisites, so a disabled Node
            # never fails on an absent secret/URL/worker capability that is
            # irrelevant while outbound sync is off (Fix Round 1, T10-I1).
            return _config_failure(
                snapshot.require_central_probe.setting_name, snapshot.require_central_probe.code
            )
        if snapshot.enabled is True and isinstance(snapshot.worker, SyncConfigurationError):
            return _config_failure(snapshot.worker.setting_name, snapshot.worker.code)
        if context.mode == "remote" and isinstance(snapshot.probe, SyncConfigurationError):
            # Task 11 (spec §5): a remote probe does not require outbound
            # sync to be enabled, but its credentials/URL must still be
            # valid typed configuration -- URL/credential failures belong
            # to SYNC_RETRY_POLICY, never to NODE_REGISTRATION.
            return _config_failure(snapshot.probe.setting_name, snapshot.probe.code)
        return DiagnosticCheck(
            "SYNC_RETRY_POLICY", "PASS", "Sync retry policy and worker prerequisites are valid."
        )

    # -- REMOTE: CENTRAL_CONNECTIVITY / CENTRAL_AUTH_ACCEPTANCE (Task 11) ----

    def _probe_once(self):
        """None when the typed probe configuration is unusable (both remote
        checks then FAIL with CONFIG_INVALID and no network call is made);
        otherwise the single CentralProbeResult for this diagnosis."""
        if isinstance(self._snapshot.probe, SyncConfigurationError):
            return None
        return probe_central(self._snapshot.probe.credentials)

    @staticmethod
    def _check_central_connectivity(result) -> DiagnosticCheck:
        if result is None:
            return DiagnosticCheck(
                "CENTRAL_CONNECTIVITY",
                "FAIL",
                "Local probe configuration is missing or invalid; no Central probe was attempted.",
                _PROBE_CONFIG_INVALID,
            )
        if result.reachable:
            details = {"http_status": result.http_status} if result.http_status is not None else {}
            return DiagnosticCheck(
                "CENTRAL_CONNECTIVITY", "PASS", "Central sync probe endpoint is reachable.", details
            )
        return DiagnosticCheck(
            "CENTRAL_CONNECTIVITY",
            "FAIL",
            "Central could not be reached (network, DNS, TLS or timeout failure).",
            _PROBE_UNREACHABLE_DETAILS,
        )

    @staticmethod
    def _check_central_auth_acceptance(result) -> DiagnosticCheck:
        if result is None:
            return DiagnosticCheck(
                "CENTRAL_AUTH_ACCEPTANCE",
                "FAIL",
                "Local probe configuration is missing or invalid; no Central probe was attempted.",
                _PROBE_CONFIG_INVALID,
            )
        if result.accepted:
            return DiagnosticCheck(
                "CENTRAL_AUTH_ACCEPTANCE",
                "PASS",
                "Central accepted the provisioned Node credential.",
                {"http_status": 200},
            )
        if not result.reachable:
            return DiagnosticCheck(
                "CENTRAL_AUTH_ACCEPTANCE",
                "FAIL",
                "Central credential acceptance could not be established: Central could not "
                "be reached.",
                _PROBE_UNREACHABLE_DETAILS,
            )
        # Reachable but not accepted: a rejection (401/403) or a response
        # that cannot prove acceptance. Neither infers whether or in what
        # state a registration exists on Central.
        code = result.error_code if result.error_code in _PROBE_FAILURE_CODES else PROBE_RESPONSE_INVALID
        details = {"error_code": code}
        if result.http_status is not None:
            details["http_status"] = result.http_status
        if code == AUTH_REJECTED:
            message = "Central rejected the provisioned Node credential."
        else:
            message = "Central's probe response could not prove credential acceptance."
        return DiagnosticCheck("CENTRAL_AUTH_ACCEPTANCE", "FAIL", message, details)
