"""Sync's own diagnostic provider (Corrective B Task 10, approved spec
§§4-6): the LOCAL checks TRUST_KEY, NODE_REGISTRATION and
SYNC_RETRY_POLICY. Tasks 11/12 add the remote/release checks.

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
from .trust import PackageTrustConfigurationError, load_package_trusted_public_key

_TRUST_KEY_INVALID = {"error_code": "TRUST_KEY_INVALID"}
_NODE_CREDENTIALS_INVALID = {"error_code": "NODE_CREDENTIALS_INVALID", "authority_verified": False}
_NODE_UNVERIFIED = {"authority_verified": False}
_REGISTRATION_INVALID_CODE = "REGISTRATION_AUTHORITY_INVALID"
_DB_UNAVAILABLE = {"error_code": "DB_UNAVAILABLE"}
_ACTIVE_ROW_CHUNK = 200


@dataclass(frozen=True)
class _SyncSettingsSnapshot:
    """Typed values or typed failures, captured once per provider instance."""

    enabled: bool | DeferredSyncSettingError
    require_central_probe: bool | DeferredSyncSettingError
    identity: UUID | SyncConfigurationError
    worker: SyncConfiguration | SyncConfigurationError


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
    return _SyncSettingsSnapshot(enabled, require_probe, identity, worker)


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
    codes = frozenset({"TRUST_KEY", "NODE_REGISTRATION", "SYNC_RETRY_POLICY"})

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
            if enabled is True:
                required.update({"NODE_REGISTRATION", "SYNC_RETRY_POLICY"})
            elif isinstance(enabled, DeferredSyncSettingError):
                # A malformed/deferred opt-in is a required configuration
                # FAIL -- never truthiness-based enablement or silent false.
                required.add("SYNC_RETRY_POLICY")
        return frozenset(required)

    def checks(self, context: DiagnosticContext) -> tuple[DiagnosticCheck, ...]:
        handlers = {
            "TRUST_KEY": self._check_trust_key,
            "NODE_REGISTRATION": self._check_node_registration,
            "SYNC_RETRY_POLICY": self._check_sync_retry_policy,
        }
        return tuple(handlers[code](context) for code in sorted(self.required_codes(context)))

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
        del context
        snapshot = self._snapshot
        if isinstance(snapshot.enabled, DeferredSyncSettingError):
            return _config_failure(snapshot.enabled.setting_name, snapshot.enabled.code)
        if isinstance(snapshot.worker, SyncConfigurationError):
            return _config_failure(snapshot.worker.setting_name, snapshot.worker.code)
        if isinstance(snapshot.require_central_probe, DeferredSyncSettingError):
            return _config_failure(
                snapshot.require_central_probe.setting_name, snapshot.require_central_probe.code
            )
        return DiagnosticCheck(
            "SYNC_RETRY_POLICY", "PASS", "Sync retry policy and worker prerequisites are valid."
        )
