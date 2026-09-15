"""Pure diagnostic contracts; providers own safe messages and actual checks.

No settings, model, key or network access belongs here. The registry stores
factories only; provider ownership is validated when fresh instances are requested.
"""

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

Status = Literal["PASS", "WARN", "FAIL"]
Profile = Literal["central", "node"]
Mode = Literal["local", "remote", "release"]
SafeScalar = str | int | bool | None

_CODES = frozenset(
    {
        "DB_CONNECTIVITY",
        "SCHEMA_STATE",
        "SIGNING_KEY",
        "TRUST_KEY",
        "NODE_REGISTRATION",
        "SYNC_RETRY_POLICY",
        "CENTRAL_CONNECTIVITY",
        "CENTRAL_AUTH_ACCEPTANCE",
    }
)
_CAPABILITIES = frozenset(
    {
        "LINUXLAB_CAN_AUTHOR",
        "LINUXLAB_CAN_IMPORT_PACKAGES",
        "LINUXLAB_CAN_SERVE_LEARNER_CONTENT",
        "LINUXLAB_CAN_RECEIVE_SYNC_EVENTS",
        "LINUXLAB_CAN_RUN_SYNC_WORKER",
    }
)
_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}


class DiagnosticContractError(ValueError):
    """Invalid diagnostic input; messages never include rejected values."""


def _require(condition: bool) -> None:
    if not condition:
        raise DiagnosticContractError("Invalid diagnostic contract.")


def _choice(value: object, choices: Iterable[str]) -> None:
    _require(type(value) is str and value in choices)


def _profile_mode(profile: Profile, mode: Mode) -> None:
    _choice(profile, ("central", "node"))
    _choice(mode, ("local", "remote", "release"))


def _json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise DiagnosticContractError("Invalid diagnostic JSON.") from None


def _bounded(text: str, limit: int) -> None:
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise DiagnosticContractError("Invalid diagnostic text.") from None
    _require(size <= limit)


def _overall(checks: Iterable["DiagnosticCheck"]) -> Status:
    return max((check.status for check in checks), key=_RANK.__getitem__, default="PASS")


@dataclass(frozen=True)
class DiagnosticCheck:
    code: str
    status: Status
    message: str
    details: Mapping[str, SafeScalar] = field(default_factory=dict)

    def __post_init__(self):
        _choice(self.code, _CODES)
        _choice(self.status, _RANK)
        _require(type(self.message) is str and len(self.message) <= 512)
        _bounded(self.message, 512 * 4)
        _require(isinstance(self.details, Mapping))
        details = dict(self.details)
        for key, value in details.items():
            _require(type(key) is str)
            if key == "error_code":
                _require(
                    type(value) is str
                    and 1 <= len(value) <= 100
                    and re.fullmatch(r"[A-Z][A-Z0-9_]*", value) is not None
                )
            elif self.code == "NODE_REGISTRATION" and key == "authority_verified":
                _require(type(value) is bool)
            elif self.code == "NODE_REGISTRATION" and key in ("node_count", "invalid_count"):
                _require(type(value) is int and value >= 0)
            elif (
                self.code in ("CENTRAL_CONNECTIVITY", "CENTRAL_AUTH_ACCEPTANCE")
                and key == "http_status"
            ):
                _require(value is None or (type(value) is int and 100 <= value <= 599))
            else:
                raise DiagnosticContractError("Invalid diagnostic details.")
        _bounded(_json(details), 4096)
        object.__setattr__(self, "details", MappingProxyType(details))


@dataclass(frozen=True)
class DiagnosticContext:
    profile: Profile
    mode: Mode
    capabilities: frozenset[str]

    def __post_init__(self):
        _profile_mode(self.profile, self.mode)
        _require(isinstance(self.capabilities, (set, frozenset, list, tuple)))
        for capability in self.capabilities:
            _choice(capability, _CAPABILITIES)
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


def _ordered_checks(checks: Iterable[DiagnosticCheck]) -> tuple[DiagnosticCheck, ...]:
    _require(not isinstance(checks, (str, bytes, Mapping)))
    try:
        values = tuple(checks)
    except TypeError:
        raise DiagnosticContractError("Invalid diagnostic checks.") from None
    _require(all(isinstance(check, DiagnosticCheck) for check in values))
    _require(len({check.code for check in values}) == len(values))
    return tuple(sorted(values, key=lambda check: check.code))


@dataclass(frozen=True)
class DiagnosticReport:
    schema_version: int
    profile: Profile
    mode: Mode
    overall_status: Status
    checks: tuple[DiagnosticCheck, ...]

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _profile_mode(self.profile, self.mode)
        _choice(self.overall_status, _RANK)
        checks = _ordered_checks(self.checks)
        _require(self.overall_status == _overall(checks))
        object.__setattr__(self, "checks", checks)
        _bounded(_json(_report_dict(self)), 65536)


def aggregate_diagnostics(
    context: DiagnosticContext, checks: Iterable[DiagnosticCheck]
) -> DiagnosticReport:
    _require(isinstance(context, DiagnosticContext))
    ordered = _ordered_checks(checks)
    return DiagnosticReport(1, context.profile, context.mode, _overall(ordered), ordered)


def _report_dict(report: DiagnosticReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "profile": report.profile,
        "mode": report.mode,
        "overall_status": report.overall_status,
        "checks": [
            {
                "code": check.code,
                "status": check.status,
                "message": check.message,
                "details": dict(check.details),
            }
            for check in report.checks
        ],
    }


def report_to_dict(report: DiagnosticReport) -> dict[str, object]:
    _require(isinstance(report, DiagnosticReport))
    return _report_dict(report)


def report_to_json(report: DiagnosticReport) -> str:
    return _json(report_to_dict(report))


def _unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise DiagnosticContractError("Invalid diagnostic JSON constant.")


def _code_set(codes: frozenset[str]) -> None:
    _require(type(codes) is frozenset)
    for code in codes:
        _choice(code, _CODES)


def validate_diagnostic_report(
    value: object, *, context: DiagnosticContext, required_codes: frozenset[str]
) -> DiagnosticReport:
    _require(isinstance(context, DiagnosticContext))
    _code_set(required_codes)
    if type(value) is str:
        _bounded(value, 65536)
        try:
            value = json.loads(
                value, object_pairs_hook=_unique_members, parse_constant=_invalid_constant
            )
        except (ValueError, RecursionError):
            raise DiagnosticContractError("Invalid diagnostic JSON.") from None
    _require(type(value) is dict)
    _require(value.keys() == {"schema_version", "profile", "mode", "overall_status", "checks"})
    _bounded(_json(value), 65536)
    _require(value["profile"] == context.profile and value["mode"] == context.mode)
    _require(type(value["checks"]) is list)
    checks = []
    for item in value["checks"]:
        _require(type(item) is dict)
        _require(
            {"code", "status", "message"} <= item.keys() <= {"code", "status", "message", "details"}
        )
        checks.append(
            DiagnosticCheck(item["code"], item["status"], item["message"], item.get("details", {}))
        )
    report = DiagnosticReport(
        value["schema_version"],
        value["profile"],
        value["mode"],
        value["overall_status"],
        tuple(checks),
    )
    _require(required_codes <= {check.code for check in report.checks})
    return report


class DiagnosticProvider(Protocol):
    codes: frozenset[str]

    def required_codes(self, context: DiagnosticContext) -> frozenset[str]: ...

    def checks(self, context: DiagnosticContext) -> tuple[DiagnosticCheck, ...]: ...


ProviderFactory = Callable[[], DiagnosticProvider]
_factories: dict[str, ProviderFactory] = {}


def register_diagnostic_provider(name: str, factory: ProviderFactory) -> None:
    """Register without constructing providers or executing their checks.

    Code ownership conflicts are rejected by diagnostic_providers, once factory
    instances exist. Registration must not load provider configuration at startup.
    """
    _require(type(name) is str and bool(name.strip()))
    _require(callable(factory))
    if name in _factories:
        _require(_factories[name] is factory)
    else:
        _factories[name] = factory


def _provider_codes(providers: Sequence[DiagnosticProvider]) -> tuple[frozenset[str], ...]:
    owned = set()
    result = []
    try:
        for provider in providers:
            _code_set(provider.codes)
            _require(callable(provider.required_codes) and callable(provider.checks))
            _require(not owned.intersection(provider.codes))
            owned.update(provider.codes)
            result.append(provider.codes)
    except Exception:  # noqa: BLE001 — provider attributes may expose configuration in errors.
        raise DiagnosticContractError("Invalid diagnostic provider ownership.") from None
    return tuple(result)


def diagnostic_providers() -> tuple[DiagnosticProvider, ...]:
    """Instantiate fresh providers; never cache configuration or check results."""
    try:
        providers = tuple(factory() for _, factory in sorted(_factories.items()))
    except Exception:  # noqa: BLE001 — never propagate secret-bearing factory exceptions.
        raise DiagnosticContractError("Diagnostic provider construction failed.") from None
    _provider_codes(providers)
    return providers


def required_check_codes(
    context: DiagnosticContext, providers: Sequence[DiagnosticProvider]
) -> frozenset[str]:
    _require(isinstance(context, DiagnosticContext))
    _require(isinstance(providers, Sequence) and not isinstance(providers, (str, bytes)))
    ownership = _provider_codes(providers)
    required = set()
    try:
        for provider, owned in zip(providers, ownership):
            codes = provider.required_codes(context)
            _code_set(codes)
            _require(codes <= owned)
            required.update(codes)
    except Exception:  # noqa: BLE001 — requirements are a module-owned failure boundary.
        raise DiagnosticContractError("Invalid diagnostic provider requirements.") from None
    return frozenset(required)
