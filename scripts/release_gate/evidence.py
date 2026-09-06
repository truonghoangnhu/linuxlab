"""Task 15 release-gate evidence schema, secret redaction, and
deterministic failure model (T15-D07).

Evidence is schema-versioned JSON plus a human-readable Markdown
projection of the same run; both are written atomically (temp file +
os.replace) so a crash mid-write never leaves a partial/corrupt
document at the target path, and both pass through a SecretRegistry
redaction pass before serialization -- any known secret VALUE
registered during the run (a DB password, a bearer token, private key
material) is scrubbed from every string it appears in, anywhere in the
document, not just from fields named like a secret.
"""

import json
import os
import re
import shutil
import tempfile
import types
import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from scripts.release_gate.environment import validate_canonical_run_id

SCHEMA_VERSION = 1

# Task 15 Task 11 corrective v5, Section 3: `\Z` (true end of string),
# never `$` -- Python's `$` also matches immediately BEFORE a single
# trailing "\n", so a `$`-anchored pattern would wrongly accept a
# well-formed digest followed by a trailing newline as "whole string
# valid". `\Z` has no such exception.
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}\Z")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
# Task 15 Task 11 corrective v7, Section 25: layer 1 of timestamp
# validation -- a strict, `\Z`-anchored structural match (date, time,
# optional fractional seconds, and either "Z" or a signed HH:MM offset)
# that also captures the offset's hour/minute digits so layer 2 can
# range-check them explicitly (datetime.fromisoformat() alone silently
# normalizes an out-of-range offset minute instead of rejecting it).
_TIMESTAMP_STRUCTURE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?P<tz>Z|(?P<tz_sign>[+-])(?P<tz_hour>\d{2}):(?P<tz_minute>\d{2}))\Z"
)
_VALID_DELIVERY_STATES = ("PENDING", "DELIVERED")
_OUTBOX_TO_INBOX_REQUIRED_KEYS = frozenset({"event_uuid", "inbox_count", "node_state", "duplicate_idempotent"})


class InvalidProofError(Exception):
    """Task 15 Task 11 corrective v2, Section 7: raised when a candidate
    proof value fails typed validation before it would otherwise be
    admitted into EvidenceV1. Field-name allowlisting alone is
    insufficient -- an allowlisted key could still carry an arbitrary
    value (a credential, an answer-key sentinel, an Authorization
    header) unless every value is itself validated against its own
    closed type. A malformed proof value must never be silently
    dropped OR silently passed through; the caller is responsible for
    turning this into a non-zero gate result."""


def validate_sha256_hex(value, *, field_name: str) -> str:
    """Sha256Hex (Section 7.1): exactly 64 lowercase hexadecimal
    characters. Applies to package_sha256, payload_sha256, and
    public_key_fingerprint.

    Task 15 Task 11 corrective v7, Section 23/24: `type(value) is str`,
    never `isinstance` -- a str SUBCLASS could carry hidden/overridden
    behavior (e.g. an overridden `__eq__`/`__hash__`, or lazily-computed
    content) while still comparing equal to a well-formed digest; exact
    type matching rejects it regardless."""
    if type(value) is not str or not _SHA256_HEX_RE.match(value):
        raise InvalidProofError(
            f"{field_name} must be a 64-character lowercase hexadecimal sha256 digest"
        )
    return value


@dataclass(frozen=True)
class SignatureProof:
    """Section 7.2: a signature proof is a typed boolean, never an
    arbitrary string or the raw verification detail."""

    verified: bool

    def __post_init__(self):
        if not isinstance(self.verified, bool):
            raise InvalidProofError("SignatureProof.verified must be a bool")

    def to_dict(self) -> dict:
        return {"verified": self.verified}


@dataclass(frozen=True)
class OutboxInboxProof:
    """Section 7.3: a typed Outbox -> Inbox delivery proof. No
    Authorization header, raw payload, raw response body, credential,
    or arbitrary detail may appear here -- only these four validated
    fields."""

    event_uuid: str
    inbox_count: int
    node_state: str
    duplicate_idempotent: bool

    def __post_init__(self):
        try:
            uuid_module.UUID(str(self.event_uuid))
        except (ValueError, AttributeError, TypeError) as exc:
            raise InvalidProofError("OutboxInboxProof.event_uuid must be a valid UUID") from exc
        if not isinstance(self.inbox_count, int) or isinstance(self.inbox_count, bool) or self.inbox_count < 0:
            raise InvalidProofError("OutboxInboxProof.inbox_count must be a non-negative int")
        # Task 15 Task 11 corrective v7, Section 23/24: exact `type(x) is
        # str`, never `isinstance`, for canonical strings.
        if type(self.node_state) is not str or self.node_state not in _VALID_DELIVERY_STATES:
            raise InvalidProofError(
                f"OutboxInboxProof.node_state must be one of {_VALID_DELIVERY_STATES}"
            )
        if not isinstance(self.duplicate_idempotent, bool):
            raise InvalidProofError("OutboxInboxProof.duplicate_idempotent must be a bool")

    def to_dict(self) -> dict:
        return {
            "event_uuid": str(self.event_uuid),
            "inbox_count": self.inbox_count,
            "node_state": self.node_state,
            "duplicate_idempotent": self.duplicate_idempotent,
        }


def _validate_outbox_mapping_shape(value) -> dict:
    """Task 15 Task 11 corrective v3, Section 11.1 / Critical finding
    #2: the EXACT key set is validated BEFORE the mapping is ever
    unpacked into OutboxInboxProof(**value). Passing an arbitrary
    mapping straight into `**value` lets Python's own TypeError (for a
    missing or unexpected keyword) escape with the offending key NAME
    embedded verbatim in its message -- an uncontrolled leak channel
    completely outside the safe-FailureRecord boundary, regardless of
    how safely everything else is handled. Every shape mismatch
    (missing key, extra key, sentinel-looking key such as
    "Authorization") normalizes to InvalidProofError here instead, with
    a message built ONLY from key COUNTS, never the actual key names or
    values."""
    if not isinstance(value, dict):
        raise InvalidProofError(
            "outbox_to_inbox must be a typed {event_uuid, inbox_count, node_state, "
            "duplicate_idempotent} mapping, not a bare string"
        )
    keys = set(value.keys())
    if keys != _OUTBOX_TO_INBOX_REQUIRED_KEYS:
        missing = len(_OUTBOX_TO_INBOX_REQUIRED_KEYS - keys)
        extra = len(keys - _OUTBOX_TO_INBOX_REQUIRED_KEYS)
        raise InvalidProofError(
            f"outbox_to_inbox mapping does not have exactly the required key set "
            f"({missing} missing, {extra} unexpected)"
        )
    return value


def _validate_bool_assertion(value, key: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidProofError(f"assertion {key!r} must be a bool")
    return value


def _validate_nonneg_int_assertion(value, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidProofError(f"assertion {key!r} must be a non-negative int")
    return value


def _validate_delivery_state_assertion(value, key: str) -> str:
    # Task 15 Task 11 corrective v7, Section 23/24: exact `type(x) is
    # str`, never `isinstance`, for canonical strings.
    if type(value) is not str or value not in _VALID_DELIVERY_STATES:
        raise InvalidProofError(f"assertion {key!r} must be one of {_VALID_DELIVERY_STATES}")
    return value


# Section 8's closed assertion-key registry, covering EVERY scenario's
# mandatory assertion set (corrective v3, Section 13). A key absent
# here is simply not release-evidence content (silently dropped, same
# as an unrecognized proof field); a key present here with a value
# that fails its own TYPE validator raises InvalidProofError rather
# than admitting an unconstrained arbitrary value -- this is syntax
# validation only. Whether a syntactically valid value is the
# semantically SUCCESSFUL one (Section 14) is checked separately by
# validate_for_pass() via _ASSERTION_EXPECTED_VALUE below.
_ASSERTION_VALIDATORS = {
    # happy_path
    "package_installed_and_activated": _validate_bool_assertion,
    "result_matches_installed_artifact": _validate_bool_assertion,
    "completion_event_created": _validate_bool_assertion,
    "event_delivered_to_central": _validate_bool_assertion,
    # n1
    "existing_active_preserved": _validate_bool_assertion,
    "active_package_identity_unchanged": _validate_bool_assertion,
    "replacement_rejected": _validate_bool_assertion,
    # n2
    "request_rejected": _validate_bool_assertion,
    "central_inbox_mutated": _validate_bool_assertion,
    "revoked_credential_not_accepted": _validate_bool_assertion,
    # n3
    "central_commit_before_ack_loss": _validate_bool_assertion,
    "node_pending_before_retry": _validate_bool_assertion,
    "final_inbox_count": _validate_nonneg_int_assertion,
    "final_node_state": _validate_delivery_state_assertion,
    "duplicate_idempotent": _validate_bool_assertion,
    # n4
    "local_runtime_available": _validate_bool_assertion,
    "outbox_remained_durable": _validate_bool_assertion,
    "no_event_loss": _validate_bool_assertion,
    "recovery_delivery_success": _validate_bool_assertion,
    # n5
    "active_package_survived_restart": _validate_bool_assertion,
    "pending_outbox_survived_restart": _validate_bool_assertion,
    "runtime_recovered": _validate_bool_assertion,
    "delivery_resumed": _validate_bool_assertion,
    # backup_restore
    "central_business_state_restored": _validate_bool_assertion,
    "node_business_state_restored": _validate_bool_assertion,
    "pending_outbox_restored": _validate_bool_assertion,
    "restored_sync_can_resume": _validate_bool_assertion,
}

# Task 15 Task 11 corrective v3, Section 16 "Scenarios": the exact
# required scenario set -- a PASS candidate must have exactly these,
# no more, no fewer. Names match scripts.release_gate.gate_runner.
# DEFAULT_SCENARIO_SPECS (the spec's own short names -- happy_path, n1,
# n2... -- are illustrative; the actual scenario identity in this
# codebase already carries a more descriptive name, and renaming it
# would be an unrelated, purely cosmetic refactor).
REQUIRED_SCENARIOS = (
    "happy_path",
    "n1_tampered_replacement",
    "n2_revoked_credential",
    "n3_ack_lost_real_proxy",
    "n4_central_unavailable",
    "n5_node_restart",
    "backup_restore_smoke",
)

# Section 13: each required scenario's EXACT mandatory assertion-key
# set. validate_for_pass() requires a PASS-eligible scenario's
# `assertions` dict to match one of these sets exactly -- neither
# missing a required key nor carrying an extra/wrong-scenario one.
REQUIRED_SCENARIO_ASSERTIONS = {
    "happy_path": frozenset(
        {
            "package_installed_and_activated",
            "result_matches_installed_artifact",
            "completion_event_created",
            "event_delivered_to_central",
        }
    ),
    "n1_tampered_replacement": frozenset(
        {"existing_active_preserved", "active_package_identity_unchanged", "replacement_rejected"}
    ),
    "n2_revoked_credential": frozenset(
        {"request_rejected", "central_inbox_mutated", "revoked_credential_not_accepted"}
    ),
    "n3_ack_lost_real_proxy": frozenset(
        {
            "central_commit_before_ack_loss",
            "node_pending_before_retry",
            "final_inbox_count",
            "final_node_state",
            "duplicate_idempotent",
        }
    ),
    "n4_central_unavailable": frozenset(
        {"local_runtime_available", "outbox_remained_durable", "no_event_loss", "recovery_delivery_success"}
    ),
    "n5_node_restart": frozenset(
        {
            "active_package_survived_restart",
            "pending_outbox_survived_restart",
            "runtime_recovered",
            "delivery_resumed",
        }
    ),
    "backup_restore_smoke": frozenset(
        {
            "central_business_state_restored",
            "node_business_state_restored",
            "pending_outbox_restored",
            "restored_sync_can_resume",
        }
    ),
}

# Section 14: the semantically SUCCESSFUL value for each mandatory
# assertion key -- most are "true", but a few (central_inbox_mutated)
# are successful precisely when FALSE, and final_inbox_count/
# final_node_state have a specific required value, not a bool at all.
_ASSERTION_EXPECTED_VALUE = {
    "package_installed_and_activated": True,
    "result_matches_installed_artifact": True,
    "completion_event_created": True,
    "event_delivered_to_central": True,
    "existing_active_preserved": True,
    "active_package_identity_unchanged": True,
    "replacement_rejected": True,
    "request_rejected": True,
    "central_inbox_mutated": False,
    "revoked_credential_not_accepted": True,
    "central_commit_before_ack_loss": True,
    "node_pending_before_retry": True,
    "final_inbox_count": 1,
    "final_node_state": "DELIVERED",
    "duplicate_idempotent": True,
    "local_runtime_available": True,
    "outbox_remained_durable": True,
    "no_event_loss": True,
    "recovery_delivery_success": True,
    "active_package_survived_restart": True,
    "pending_outbox_survived_restart": True,
    "runtime_recovered": True,
    "delivery_resumed": True,
    "central_business_state_restored": True,
    "node_business_state_restored": True,
    "pending_outbox_restored": True,
    "restored_sync_can_resume": True,
}


def build_typed_assertions(detail) -> dict:
    """Task 15 Task 11 corrective v2, Section 8: the ONE place a
    scenario's raw returned detail dict is allowed to contribute to
    ScenarioResult.assertions. Mirrors build_typed_proofs()'s
    allowlist-plus-per-value-validation shape."""
    assertions: dict = {}
    if not isinstance(detail, dict):
        return assertions
    for key, validator in _ASSERTION_VALIDATORS.items():
        if key in detail:
            assertions[key] = validator(detail[key], key)
    return assertions


def build_typed_proofs(*raw_detail_dicts: dict) -> dict:
    """The ONE place a scenario's raw returned detail dict is allowed to
    contribute to EvidenceV1.proofs (Section 7 / Section 18.1). Every
    candidate value is validated against its own typed schema before
    admission; a malformed value raises InvalidProofError rather than
    being silently dropped or passed through unsafely. A key absent
    from the fixed allowlist below is silently ignored (never an
    error) -- it is simply not release-evidence content, e.g. a
    scenario's own internal bookkeeping field such as `event_id`.
    First-seen-wins across multiple detail dicts, exactly like the
    prior allowlist-only extraction this replaces."""
    proofs: dict = {}
    for detail in raw_detail_dicts:
        if not isinstance(detail, dict):
            continue
        if "package_sha256" in detail and "package_sha256" not in proofs:
            proofs["package_sha256"] = validate_sha256_hex(detail["package_sha256"], field_name="package_sha256")
        if "payload_sha256" in detail and "payload_sha256" not in proofs:
            proofs["payload_sha256"] = validate_sha256_hex(detail["payload_sha256"], field_name="payload_sha256")
        if "public_key_fingerprint" in detail and "public_key_fingerprint" not in proofs:
            proofs["public_key_fingerprint"] = validate_sha256_hex(
                detail["public_key_fingerprint"], field_name="public_key_fingerprint"
            )
        if "signature_verified" in detail and "signature" not in proofs:
            proofs["signature"] = SignatureProof(verified=detail["signature_verified"]).to_dict()
        if "outbox_to_inbox" in detail and "outbox_to_inbox" not in proofs:
            shape_checked = _validate_outbox_mapping_shape(detail["outbox_to_inbox"])
            proofs["outbox_to_inbox"] = OutboxInboxProof(**shape_checked).to_dict()
    return proofs

EXIT_CODE = {
    "PASS": 0,
    "PRECHECK_FAILURE": 2,
    "STARTUP_FAILURE": 3,
    "SCENARIO_FAILURE": 4,
    "CLEANUP_FAILURE": 5,
}

_REDACTED = "[REDACTED]"


class RedactionKeyCollisionError(Exception):
    """Raised when redacting two distinct dict keys would produce the
    same sanitized key. Checkpoint corrective (finding #3): silently
    letting one redacted entry overwrite another could both hide data
    and make evidence internally inconsistent, so this fails closed
    instead of picking a winner."""


class EvidencePublicationError(Exception):
    """Task 15 Task 11 corrective v2, Section 13: raised when the final
    evidence directory already exists (fail-closed collision -- never
    an overwrite) or when publication otherwise cannot proceed."""


class StagingCleanupError(Exception):
    """Task 15 Task 11 corrective v4, Section 28: raised when evidence
    publication fails AND the subsequent removal of the private staging
    directory ALSO fails, leaving residue on disk -- distinct from a
    plain publication failure (where staging is cleanly removed) so the
    caller can tell genuine residue was left behind. The ORIGINAL
    publication failure is always available as `__cause__` (explicit
    `raise ... from original_exc`, not relied on via implicit
    `__context__` tracking, which would instead follow the innermost
    -- cleanup's own -- exception at this point)."""


class SecretLeakError(Exception):
    """Raised when a final full-text scan of serialized evidence output
    still contains a registered secret. Checkpoint corrective (finding
    #3): redact_value()/redact_text() are defense in depth, not proof;
    this is the last-resort check that aborts the write entirely rather
    than ever publish a partially-redacted artifact."""


class SecretRegistry:
    """Accumulates known secret VALUES seen during a gate run so
    redact_text()/redact_value() can scrub every occurrence of each
    exact value from evidence/log output, wherever it appears -- in
    string values AND in dict keys, since a secret can end up embedded
    in a key (e.g. an identifier built from a token). This is defense
    in depth, never a substitute for not putting secrets in evidence in
    the first place."""

    def __init__(self):
        self._secrets: set[str] = set()

    def register(self, value) -> None:
        if value:
            self._secrets.add(str(value))

    def redact_text(self, text: str) -> str:
        # Longest-first: if one registered secret is a substring of
        # another (e.g. "ABC" and "ABCDEF"), redacting the shorter one
        # first would consume part of the longer one and leave its
        # remainder ("DEF") exposed. Sorting by descending length makes
        # redaction order deterministic and immune to that case, instead
        # of depending on set iteration order.
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, _REDACTED)
        return text

    def redact_value(self, value):
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            result: dict = {}
            for key, item in value.items():
                new_key = self.redact_text(key) if isinstance(key, str) else key
                if new_key in result:
                    raise RedactionKeyCollisionError(
                        f"redacting dict key {key!r} produced {new_key!r}, which "
                        "collides with another key already redacted in this same "
                        "mapping -- refusing to silently overwrite one entry"
                    )
                result[new_key] = self.redact_value(item)
            return result
        if isinstance(value, (list, tuple)):
            return [self.redact_value(item) for item in value]
        return value

    def assert_no_secrets_present(self, text: str) -> None:
        for secret in self._secrets:
            if secret in text:
                raise SecretLeakError(
                    "a registered secret is still present in serialized evidence "
                    "output after redaction -- refusing to write this artifact"
                )


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    status: str  # "PASS" | "FAIL"
    assertions: dict = field(default_factory=dict)
    duration_ms: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class FailureRecord:
    """Task 15 Task 11 corrective, Critical: the evidence security
    boundary. `code` is the ONLY exception-derived content ever
    admitted here -- an exception class name is a fixed identifier
    chosen by the code that defines it, so it can never itself carry
    dynamic/sensitive content, unlike `str(exc)`/`repr(exc)` (which
    Evidence never serializes directly -- see gate_runner._sanitize_
    exception()). `safe_message` is built from an explicit safe
    template (category/stage/code only), never from raw exception
    text, so an unanticipated sensitive value embedded in some
    exception's own message (an answer-key sentinel, a stdin payload)
    structurally cannot reach evidence -- there is no reliance on that
    specific value having been pre-registered with SecretRegistry."""

    category: str  # "PRECHECK_FAILURE" | "STARTUP_FAILURE" | "SCENARIO_FAILURE" | "CLEANUP_FAILURE"
    stage: str  # scenario name, or "preflight" / "startup" / "cleanup"
    code: str  # exception type name
    safe_message: str

    def __post_init__(self):
        # Task 15 Task 11 corrective v12, Section 7: every field here is
        # authoritative terminal-evidence content once embedded in
        # ReleaseEvidence.{primary,cleanup,finalization}_failure -- sealed
        # at construction, independent of whether/how it is later
        # embedded, exactly like ReleaseEvidence's own top-level scalars.
        validate_exact_type(self.category, str, field_name="FailureRecord.category")
        validate_exact_type(self.stage, str, field_name="FailureRecord.stage")
        validate_exact_type(self.code, str, field_name="FailureRecord.code")
        validate_exact_type(self.safe_message, str, field_name="FailureRecord.safe_message")

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "stage": self.stage,
            "code": self.code,
            "safe_message": self.safe_message,
        }


class UnsupportedEvidenceValueError(Exception):
    """Task 15 Task 11 corrective v10, Section 9: raised when
    `deep_freeze()` encounters a value that is neither a known-safe
    immutable scalar (str/int/float/bool/None/bytes) nor one of the
    recursively-handled container types (any `collections.abc.Mapping`,
    `list`/`tuple`, `set`/`frozenset`) -- e.g. a custom class instance
    with mutable attributes. Such a value cannot be safely detached/
    frozen, so snapshot construction is rejected rather than silently
    admitting an opaque mutable reference into supposedly-immutable
    evidence."""


# Task 15 Task 11 corrective v10, Section 8/9 (hardened by corrective
# v11, Section 5; narrowed by corrective v12, Section 4/10): the closed
# set of leaf value types `deep_freeze()` ever returns unchanged --
# every real call site in this codebase (FailureRecord.to_dict(),
# scenario details, proof/assertion values, environment/validation
# metadata) only ever produces these plus the recursively-handled
# container types, so this check is a narrowly-scoped defensive
# backstop, not a general-purpose type system. `bytes` was removed here
# by corrective v12 -- it was never part of the canonical terminal
# scalar algebra (Section 4: str/int/float/bool/None only) and no real
# call site ever actually produced it; retaining it as "safe" would
# have let raw bytes reach published JSON/Markdown evidence unnoticed.
_SAFE_LEAF_TYPES = (str, int, float, bool, type(None))


def validate_exact_type(value, expected_type: type, *, field_name: str):
    """Task 15 Task 11 corrective v12, Section 5/6/7: rejects anything
    that is not an EXACT instance of `expected_type` -- never
    `isinstance()`, which a subclass (carrying hidden mutable state or
    overridden `__eq__`/`__str__` behavior while still comparing/
    printing as the plain builtin it merely resembles) would satisfy.
    `expected_type is int` also correctly rejects `bool` (a `True`/
    `False` passed where an exact `int` is required) since `type(True)
    is bool`, never `int`, even though `isinstance(True, int)` is True.
    Never coerces via `str(value)`/`int(value)` -- an unsupported value
    fails construction rather than being silently normalized."""
    if type(value) is not expected_type:
        raise UnsupportedEvidenceValueError(
            f"{field_name} must be an exact {expected_type.__name__}, "
            f"got {type(value).__name__!r}"
        )
    return value


def _is_exact_safe_leaf(value) -> bool:
    """Task 15 Task 11 corrective v11, Section 5: `type(value) in
    _SAFE_LEAF_TYPES`, never `isinstance(value, _SAFE_LEAF_TYPES)` -- a
    `str`/`int` SUBCLASS (e.g. a `MutableString(str)` carrying hidden
    mutable attributes or overridden `__eq__`/`__hash__`/`__str__`
    behavior) satisfies `isinstance` while still comparing/printing as
    if it were the plain builtin, so only an EXACT type match closes
    this -- `type(x)` is the subclass itself, never `in` the fixed
    tuple of real builtin type objects."""
    return type(value) in _SAFE_LEAF_TYPES


def _validate_canonical_mapping_key(key) -> None:
    """Task 15 Task 11 corrective v11, Section 6/8: every terminal
    mapping key must be an exact canonical `str` -- never a `str`
    subclass, a custom hashable object, or any other caller-controlled
    key object. Rejected outright rather than normalized with
    `str(key)`, which would silently discard the distinction between a
    genuine string key and an attacker-supplied stand-in whose
    `__hash__`/`__eq__` could still be mutated by the caller after this
    call returns (the key-analogue of corrective v10's value-alias
    attack -- Section 8's "mapping-key alias attack must fail")."""
    if type(key) is not str:
        raise UnsupportedEvidenceValueError(
            f"deep_freeze() encountered a mapping key of type {type(key).__name__!r} "
            "that is not an exact canonical str"
        )


def deep_freeze(value):
    """Task 15 Task 11 corrective v9, Section 5/6 (hardened by
    corrective v10, Section 7-9): recursively converts a value into an
    immutable, fully-DETACHED equivalent -- any `collections.abc.
    Mapping` (a plain `dict`, a `types.MappingProxyType`, or a custom
    Mapping implementation alike) becomes a `types.MappingProxyType`
    wrapping a BRAND NEW dict whose values are each recursively frozen
    (this both deep-copies/detaches from the original mutable input AND
    makes item assignment raise `TypeError`, regardless of what kind of
    Mapping it started life as -- `.items()` is part of the Mapping ABC
    contract); `list`/`tuple` become a tuple of recursively-frozen
    items; `set`/`frozenset` become a `frozenset`; a known-safe scalar
    (str/int/float/bool/None/bytes) is returned unchanged. Idempotent
    and safe to call on an already-frozen value (e.g. re-freezing a
    field copied from a previously-built ReleaseEvidence): a
    `MappingProxyType` is itself a Mapping, so it is simply rebuilt into
    a new, equivalent, still-detached one -- redundant but harmless.

    A read-only wrapper over an ALIASED mutable dict is NOT sufficient
    on its own (spec Section 7/11) -- `isinstance(value, dict)` alone
    would miss a `types.MappingProxyType` (it is not a `dict` subclass),
    letting that exact aliased, caller-owned backing dict flow through
    unchanged. Checking `isinstance(value, Mapping)` (the ABC) instead,
    and always rebuilding via the dict COMPREHENSION below, guarantees a
    genuinely new backing dict for every nested mapping regardless of
    its concrete type, so mutating the ORIGINAL input (or its backing
    dict, if it was a proxy) after this call can never be reflected
    here.

    Task 15 Task 11 corrective v10, Section 9: a value that is neither a
    Mapping/list/tuple/set/frozenset nor a known-safe scalar (e.g. a
    custom class instance with mutable attributes) cannot be safely
    detached/frozen -- rather than returning it unchanged as if it were
    safe, this raises UnsupportedEvidenceValueError so snapshot
    construction fails closed instead of silently retaining an opaque
    mutable reference.

    Task 15 Task 11 corrective v11, Section 5/6/8/9: hardened further --
    every mapping KEY is validated as an exact canonical `str` before
    its value is even recursed into (_validate_canonical_mapping_key(),
    rejecting a `str` subclass or a custom/mutable hashable key exactly
    like the ORIGINAL v10 value-alias attack, but for keys), and every
    scalar LEAF is required to be an EXACT builtin type
    (_is_exact_safe_leaf(), never `isinstance`) -- a `str`/`int`
    subclass carrying hidden mutable attributes or overridden behavior
    is rejected here rather than silently retained as if it were the
    plain builtin it merely resembles."""
    if isinstance(value, Mapping):
        frozen_items = {}
        for key, item in value.items():
            _validate_canonical_mapping_key(key)
            frozen_items[key] = deep_freeze(item)
        return types.MappingProxyType(frozen_items)
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item) for item in value)
    if _is_exact_safe_leaf(value):
        return value
    raise UnsupportedEvidenceValueError(
        f"deep_freeze() encountered an unsupported mutable value of type "
        f"{type(value).__name__!r} that cannot be safely detached/frozen"
    )


def _thaw(value):
    """The renderer-conversion counterpart to deep_freeze() (spec
    Section 9, hardened by corrective v12 Section 11): recursively
    converts a deep-frozen value back into fresh, disposable, genuinely
    JSON-serializable plain data -- `types.MappingProxyType` -> a brand
    new plain `dict`, `tuple`/`frozenset` -> a brand new plain `list`.
    The result shares no mutable container with the frozen snapshot it
    was built from, so mutating it can never mutate the snapshot back
    (Section 9: "those plain structures are disposable and
    non-authoritative").

    Task 15 Task 11 corrective v12, Section 11: fails closed -- every
    value reaching this function was itself produced by deep_freeze(),
    so it can only ever be a MappingProxyType, a tuple/frozenset, or an
    exact safe scalar; anything else indicates a value that bypassed
    deep_freeze() entirely (e.g. a field assigned after construction via
    object.__setattr__, or a future field that forgets to route through
    it) and is rejected here rather than passed through unchanged --
    there is no `return value` fallback for an unrecognized type."""
    if isinstance(value, types.MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, frozenset)):
        return [_thaw(item) for item in value]
    if _is_exact_safe_leaf(value):
        return value
    raise UnsupportedEvidenceValueError(
        f"_thaw() encountered an unsupported terminal export value of type "
        f"{type(value).__name__!r}"
    )


@dataclass(frozen=True)
class ReleaseEvidence:
    """Task 15 Task 11 corrective v2 (deeply immutable as of corrective
    v9): the ONE canonical in-memory evidence record (EvidenceV1,
    schema_version 1). Both write_json_evidence() and
    write_markdown_evidence() render from this same object -- never two
    independently-maintained partial schemas.

    `retention` replaces the prior `retained_resources` field
    (corrective v2, Section 3): execution resources (databases,
    workspace, `.llpkg`, keys, dumps, logs) are NEVER retained
    regardless of --keep-on-failure or failure stage --
    `execution_resources_retained` is therefore always False. Only the
    sanitized evidence pair itself may ever be described as retained
    (`sanitized_debug_bundle_retained`).

    Task 15 Task 11 corrective v9, Section 4-8: the dataclass itself is
    frozen (blocking top-level field reassignment), and every mutable-
    typed field is deep-frozen (via deep_freeze(), in __post_init__)
    the instant an instance is constructed -- regardless of which call
    site builds it (gate_runner._build_snapshot(), or a test/candidate
    constructed directly with plain dict literals). This is the ONE
    place detachment/immutability is enforced, so no decision-bearing
    mutable reference can ever be shared with a scenario result, proof
    dict, validation dict, retention dict, environment metadata, or any
    other caller-owned structure once an instance exists."""

    schema_version: int
    run: dict  # run_id, git_sha, started_at, finished_at, authority
    environment: dict  # platform, python_version, django_version, postgres_version
    profiles: dict  # central, node (settings module dotted paths)
    validation: dict  # clean_tree, central_check, node_check, central_migration_drift, node_migration_drift
    scenarios: dict  # scenario name -> ScenarioResult fields (as dict)
    proofs: dict  # package_sha256, payload_sha256, public_key_fingerprint, signature, outbox_to_inbox
    retention: dict  # keep_on_failure_requested, execution_resources_retained (always False), sanitized_debug_bundle_retained
    primary_failure: dict | None  # FailureRecord.to_dict(), or None on PASS
    cleanup_failure: dict | None  # FailureRecord.to_dict(), or None if cleanup fully succeeded
    finalization_failure: dict | None  # FailureRecord.to_dict(), or None if evidence publication fully succeeded
    cleanup_status: str  # e.g. "clean" | "failed"
    overall: str
    exit_code: int

    def __post_init__(self):
        # Task 15 Task 11 corrective v12, Section 5/8: top-level terminal
        # scalar fields are never dict/Mapping-shaped, so they never went
        # through deep_freeze() below at all -- sealed here, first,
        # BEFORE any nested field is touched, so a construction failure
        # on one of these never depends on nested-field processing order.
        validate_exact_type(self.schema_version, int, field_name="ReleaseEvidence.schema_version")
        validate_exact_type(self.cleanup_status, str, field_name="ReleaseEvidence.cleanup_status")
        validate_exact_type(self.overall, str, field_name="ReleaseEvidence.overall")
        validate_exact_type(self.exit_code, int, field_name="ReleaseEvidence.exit_code")
        # object.__setattr__ is required here -- the dataclass itself is
        # frozen, so ordinary attribute assignment (even from inside
        # __post_init__) would raise dataclasses.FrozenInstanceError.
        object.__setattr__(self, "run", deep_freeze(self.run))
        object.__setattr__(self, "environment", deep_freeze(self.environment))
        object.__setattr__(self, "profiles", deep_freeze(self.profiles))
        object.__setattr__(self, "validation", deep_freeze(self.validation))
        object.__setattr__(self, "scenarios", deep_freeze(self.scenarios))
        object.__setattr__(self, "proofs", deep_freeze(self.proofs))
        object.__setattr__(self, "retention", deep_freeze(self.retention))
        object.__setattr__(self, "primary_failure", deep_freeze(self.primary_failure))
        object.__setattr__(self, "cleanup_failure", deep_freeze(self.cleanup_failure))
        object.__setattr__(self, "finalization_failure", deep_freeze(self.finalization_failure))

    def to_dict(self) -> dict:
        """The renderer-conversion boundary (spec Section 9): returns a
        BRAND NEW, fully mutable, disposable plain-data structure every
        call -- mutating the returned dict can never mutate this frozen
        snapshot back, and it is always genuinely JSON-serializable
        (no MappingProxyType/tuple left over from deep_freeze())."""
        return {
            "schema_version": self.schema_version,
            "run": _thaw(self.run),
            "environment": _thaw(self.environment),
            "profiles": _thaw(self.profiles),
            "validation": _thaw(self.validation),
            "scenarios": _thaw(self.scenarios),
            "proofs": _thaw(self.proofs),
            "retention": _thaw(self.retention),
            "primary_failure": _thaw(self.primary_failure),
            "cleanup_failure": _thaw(self.cleanup_failure),
            "finalization_failure": _thaw(self.finalization_failure),
            "cleanup_status": self.cleanup_status,
            "overall": self.overall,
            "exit_code": self.exit_code,
        }


class InvalidAssertionError(Exception):
    """Task 15 Task 11 corrective v3, Section 17: a scenario's typed
    assertions dict does not match its own EXACT required key set
    (REQUIRED_SCENARIO_ASSERTIONS) -- missing, extra, or belonging to a
    different scenario entirely. Never reflects the actual keys."""


class IncompleteEvidenceError(Exception):
    """Task 15 Task 11 corrective v3, Section 17: required EvidenceV1
    metadata (run identity, environment versions, profiles, the exact
    required scenario set) is missing or unresolved (e.g. still
    "unknown"). Never reflects the actual candidate content."""


class EvidenceInvariantError(Exception):
    """Task 15 Task 11 corrective v3, Section 17: a syntactically valid
    EvidenceV1 candidate fails a mandatory SEMANTIC release invariant
    (e.g. a proof reports verified=False, cleanup_status is not clean).
    Never reflects the actual candidate content -- see PassDecision.reasons
    for the closed set of safe, fixed reason codes."""


@dataclass(frozen=True)
class PassDecision:
    """Task 15 Task 11 corrective v3, Section 3/15: the ONLY kind of
    value that may certify an EvidenceV1 candidate as PASS-worthy.
    `reasons` is a tuple of fixed, safe, short reason codes (never raw
    candidate content) describing every mandatory invariant that was
    NOT satisfied; empty iff `eligible` is True."""

    eligible: bool
    reasons: tuple


def _is_valid_iso8601_timestamp(value) -> bool:
    """Task 15 Task 11 corrective v6/v7, Section 7/25: three independent
    layers, ALL of which must pass:

    1. strict full-string structural match (`_TIMESTAMP_STRUCTURE_RE`,
       `\\Z`-anchored -- rejects a trailing newline/junk, and rejects
       anything that isn't exactly date/time/offset shaped);
    2. explicit timezone-offset-component RANGE validation (0-59 for
       the minute component) -- `datetime.fromisoformat()` alone is
       NOT sufficient here: it silently NORMALIZES an out-of-range
       offset minute like "+00:99" into a different, validly-formed
       offset ("+01:39") instead of raising, so a value that layer 1's
       structural match accepts could still carry a bogus offset that
       only an explicit range check catches;
    3. semantic datetime parsing (`datetime.fromisoformat()`) -- catches
       calendar-impossible values (e.g. "2026-02-30", hour 25) that a
       format-only regex would wrongly accept, and confirms the result
       is timezone-AWARE (a parseable but naive timestamp, with no
       offset/`Z`, is still rejected)."""
    if type(value) is not str:
        return False

    match = _TIMESTAMP_STRUCTURE_RE.match(value)
    if match is None:
        return False

    if match.group("tz") != "Z":
        offset_hour = int(match.group("tz_hour"))
        offset_minute = int(match.group("tz_minute"))
        if not (0 <= offset_hour <= 23) or not (0 <= offset_minute <= 59):
            return False

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_valid_required_string(value) -> bool:
    """Task 15 Task 11 corrective v5, Section 3: a strict non-empty,
    non-"unknown" STRING -- `type(value) is str` first, so a bool (a
    subclass of int, never of str) or int can never satisfy this merely
    by being truthy."""
    return type(value) is str and value != "" and value != "unknown"


def _validate_for_pass_metadata(candidate: ReleaseEvidence, reasons: list) -> None:
    # Task 15 Task 11 corrective v6, Section 7: `type(x) is int` first --
    # `True == 1` in Python, so a bare `!=` comparison against
    # SCHEMA_VERSION would wrongly accept the bool True (bool is an int
    # subclass, but never `type(x) is int`).
    if type(candidate.schema_version) is not int or candidate.schema_version != SCHEMA_VERSION:
        reasons.append("schema_version_mismatch")
    run = candidate.run or {}
    run_id = run.get("run_id")
    if not _is_valid_required_string(run_id):
        reasons.append("run_id_missing")
    # Task 15 Task 11 corrective v9, Section 24/28: the final-validator
    # boundary independently re-applies the SAME canonical run-ID
    # grammar the builder (environment.assert_valid_run_id()) enforces
    # -- never merely trusting that a candidate went through the
    # builder. This is deliberately a SEPARATE check from
    # `_is_valid_required_string` above (not an `elif`): a candidate
    # that bypasses the builder entirely (e.g. hand-constructed with
    # run_id="../bad") is a non-empty, non-"unknown" string that would
    # otherwise satisfy the generic check while still being
    # filesystem/DB-identifier unsafe.
    if not validate_canonical_run_id(run_id):
        reasons.append("run_id_malformed")
    git_sha = run.get("git_sha")
    # Task 15 Task 11 corrective v4, Section 10 (hardened by corrective
    # v5, Section 3): "exact valid git SHA representation used by this
    # codebase" -- not merely non-empty, and not merely `$`-anchored
    # (which would admit a trailing newline). environment.resolve_git_
    # sha() always returns exactly 40 lowercase hex characters (a real
    # `git rev-parse HEAD`); anything else is not a valid SHA this
    # codebase could have actually produced.
    if type(git_sha) is not str or not _GIT_SHA_RE.match(git_sha):
        reasons.append("git_sha_missing_or_malformed")
    if not _is_valid_iso8601_timestamp(run.get("started_at")):
        reasons.append("started_at_missing_or_malformed")
    if not _is_valid_iso8601_timestamp(run.get("finished_at")):
        reasons.append("finished_at_missing_or_malformed")

    authority = run.get("authority")
    # Task 15 Task 11 corrective v6, Section 7: authority.platform is
    # validated as a strict non-empty string in its own right here --
    # never merely inferred from the authoritative/reason branches
    # below, which would leave a non-string platform value (e.g. `True`)
    # on the authoritative=False side undetected as long as `reason`
    # happened to already be correct.
    # Task 15 Task 11 corrective v9, Section 5: `candidate.run["authority"]`
    # is a deep-frozen `types.MappingProxyType`, not a plain `dict`, once
    # ReleaseEvidence.__post_init__ has run -- `isinstance(x, Mapping)`
    # (the ABC), never `isinstance(x, dict)`, is required here so this
    # check keeps working against the frozen representation.
    if (
        not isinstance(authority, Mapping)
        or "authoritative" not in authority
        or not _is_valid_required_string(authority.get("platform"))
    ):
        reasons.append("authority_missing")
    elif authority.get("authoritative") is True:
        if authority.get("platform") != "linux" or authority.get("reason") is not None:
            reasons.append("authority_inconsistent")
    elif authority.get("authoritative") is False:
        if authority.get("reason") != "development_only_platform":
            reasons.append("authority_inconsistent")
    else:
        reasons.append("authority_missing")

    environment = candidate.environment or {}
    for key in ("platform", "python_version", "django_version", "postgres_version"):
        if not _is_valid_required_string(environment.get(key)):
            reasons.append(f"environment_{key}_unknown")

    profiles = candidate.profiles or {}
    if not _is_valid_required_string(profiles.get("central")) or not _is_valid_required_string(profiles.get("node")):
        reasons.append("profiles_missing")


def _validate_for_pass_validation_checks(candidate: ReleaseEvidence, reasons: list) -> None:
    validation = candidate.validation or {}
    if validation.get("clean_tree") is not True:
        reasons.append("clean_tree_not_true")
    if validation.get("central_check") != "ok":
        reasons.append("central_check_not_ok")
    if validation.get("node_check") != "ok":
        reasons.append("node_check_not_ok")
    if validation.get("central_migration_drift") != "none":
        reasons.append("central_migration_drift_detected")
    if validation.get("node_migration_drift") != "none":
        reasons.append("node_migration_drift_detected")


def _value_matches_expected(value, expected) -> bool:
    """Task 15 Task 11 corrective v4, Section 18/21: a STRICT typed
    comparison, never bare equality -- `1 == True` and `0 == False` in
    Python, so a naive `value != expected` would wrongly accept an
    integer substitute for a required bool. `type(value) is type(
    expected)` rejects that (and, since `type(True) is bool`, also
    correctly rejects bool where an int like `final_inbox_count` is
    expected -- bool is an int subclass, but never `type() is int`)."""
    return type(value) is type(expected) and value == expected


def _validate_for_pass_scenarios(candidate: ReleaseEvidence, reasons: list) -> None:
    scenarios = candidate.scenarios or {}
    if set(scenarios.keys()) != set(REQUIRED_SCENARIOS):
        reasons.append("required_scenario_set_mismatch")
        return
    for name in REQUIRED_SCENARIOS:
        result = scenarios[name]
        # Task 15 Task 11 corrective v9, Section 5: `result`/`assertions`
        # are deep-frozen `types.MappingProxyType` values, not plain
        # `dict` -- `isinstance(x, Mapping)` (the ABC) is required.
        if not isinstance(result, Mapping) or result.get("status") != "PASS":
            reasons.append(f"scenario_{name}_not_pass")
            continue
        required_keys = REQUIRED_SCENARIO_ASSERTIONS[name]
        assertions = result.get("assertions")
        if not isinstance(assertions, Mapping) or set(assertions.keys()) != required_keys:
            reasons.append(f"scenario_{name}_assertion_set_mismatch")
            continue
        for key in required_keys:
            if not _value_matches_expected(assertions.get(key), _ASSERTION_EXPECTED_VALUE[key]):
                reasons.append(f"scenario_{name}_assertion_{key}_not_successful")


def _validate_for_pass_proofs(candidate: ReleaseEvidence, reasons: list) -> None:
    """Task 15 Task 11 corrective v4, Section 8/12: validate_for_pass()
    is the AUTHORITATIVE validation boundary -- every proof field is
    re-validated for format/type here independently, never merely
    trusted because it is already present in `candidate.proofs`. A
    candidate built by some other path than build_typed_proofs() (a
    bug, a hand-built object, a future code path) must still be caught
    here rather than silently admitted."""
    proofs = candidate.proofs or {}
    for key in ("package_sha256", "payload_sha256", "public_key_fingerprint"):
        value = proofs.get(key)
        if type(value) is not str or not _SHA256_HEX_RE.match(value):
            reasons.append(f"proof_{key}_missing_or_malformed")

    signature = proofs.get("signature")
    # Task 15 Task 11 corrective v9, Section 5: `Mapping`, not `dict` --
    # these are deep-frozen `types.MappingProxyType` values.
    if (
        not isinstance(signature, Mapping)
        or set(signature.keys()) != {"verified"}
        or type(signature.get("verified")) is not bool
    ):
        reasons.append("proof_signature_malformed")
    elif signature["verified"] is not True:
        reasons.append("proof_signature_not_verified")

    outbox = proofs.get("outbox_to_inbox")
    if not isinstance(outbox, Mapping) or set(outbox.keys()) != _OUTBOX_TO_INBOX_REQUIRED_KEYS:
        reasons.append("proof_outbox_to_inbox_malformed")
    else:
        try:
            uuid_module.UUID(str(outbox.get("event_uuid")))
            event_uuid_valid = True
        except (ValueError, AttributeError, TypeError):
            event_uuid_valid = False
        if not event_uuid_valid:
            reasons.append("proof_outbox_event_uuid_invalid")
        if type(outbox.get("inbox_count")) is not int or outbox.get("inbox_count") != 1:
            reasons.append("proof_outbox_inbox_count_not_1")
        if type(outbox.get("node_state")) is not str or outbox.get("node_state") != "DELIVERED":
            reasons.append("proof_outbox_node_state_not_delivered")
        if type(outbox.get("duplicate_idempotent")) is not bool or outbox.get("duplicate_idempotent") is not True:
            reasons.append("proof_outbox_not_duplicate_idempotent")


def _validate_for_pass_cleanup(candidate: ReleaseEvidence, reasons: list) -> None:
    retention = candidate.retention or {}
    if retention.get("execution_resources_retained") is not False:
        reasons.append("execution_resources_retained")
    if candidate.cleanup_status != "clean":
        reasons.append("cleanup_status_not_clean")
    if candidate.primary_failure is not None:
        reasons.append("primary_failure_present")
    if candidate.cleanup_failure is not None:
        reasons.append("cleanup_failure_present")
    if candidate.finalization_failure is not None:
        reasons.append("finalization_failure_present")


def validate_for_pass(candidate: ReleaseEvidence) -> PassDecision:
    """Task 15 Task 11 corrective v3, Section 3/16: the ONE function
    allowed to certify an EvidenceV1 candidate as PASS-worthy. No other
    code path may set `overall="PASS"`/`exit_code=0` without this
    succeeding first (see gate_runner._finish()). Checks every
    mandatory release invariant from Section 16 -- metadata,
    validation, the exact required scenario/assertion sets with
    semantically successful values, the exact required proofs with
    semantically successful values, and a clean cleanup/retention
    state -- and returns a PassDecision whose `reasons` are fixed safe
    codes, never raw candidate content."""
    reasons: list = []
    _validate_for_pass_metadata(candidate, reasons)
    _validate_for_pass_validation_checks(candidate, reasons)
    _validate_for_pass_scenarios(candidate, reasons)
    _validate_for_pass_proofs(candidate, reasons)
    _validate_for_pass_cleanup(candidate, reasons)
    return PassDecision(eligible=not reasons, reasons=tuple(reasons))


def _atomic_write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-release-evidence-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


def write_json_evidence(path, evidence: ReleaseEvidence, secrets: SecretRegistry) -> None:
    text = render_json_evidence(evidence, secrets)
    _atomic_write_text(path, text)


def publish_evidence(
    evidence_dir, evidence: ReleaseEvidence, secrets: SecretRegistry, resource_ledger=None
) -> tuple[Path, Path]:
    """Task 15 Task 11 corrective v2, Section 13: publishes the
    JSON+Markdown evidence pair as a single all-or-nothing operation.

    Both renderings are fully built and secret-scanned IN MEMORY first
    (render_json_evidence()/render_markdown_evidence() each raise
    SecretLeakError rather than return unsafe text). Only then is a
    private staging directory created next to `evidence_dir` and both
    files written into it; the staging directory is atomically renamed
    into place as `evidence_dir` as the final step. If `evidence_dir`
    already exists, this fails closed -- an existing evidence
    directory is never overwritten. Any failure at any step (a write
    failure, a secret leak, a rename failure) removes the staging
    directory before re-raising, so a half-published pair -- a lone
    `release-evidence.json` with no matching `.md`, or vice versa -- is
    never observable at the final location.

    `resource_ledger` (Task 15 Task 11 corrective v9, Section 17):
    an optional shared `gate_runner.ResourceLedger` -- when given, this
    function acquires ownership of the staging directory the INSTANT
    its physical creation succeeds (never only retroactively, on the
    specific StagingCleanupError branch below), using the exact same
    fixed safe_id gate_runner.py has always used for this resource
    ("evidence_staging"), and releases it immediately after either the
    atomic rename succeeds OR a post-failure cleanup verifiably removes
    the staging directory -- never on the branch where that removal
    itself also fails (genuine residue correctly stays owned). Defaults
    to None, preserving the exact prior behavior (no ledger involvement
    at all) for every existing caller that does not pass one."""
    evidence_dir = Path(evidence_dir)
    if evidence_dir.exists():
        raise EvidencePublicationError(f"release evidence directory already exists: {evidence_dir}")

    json_text = render_json_evidence(evidence, secrets)
    markdown_text = render_markdown_evidence(evidence, secrets)

    evidence_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = evidence_dir.parent / f".{evidence_dir.name}.staging-{os.getpid()}-{uuid_module.uuid4().hex[:8]}"
    try:
        staging_dir.mkdir(parents=True, exist_ok=False)
        if resource_ledger is not None:
            resource_ledger.acquire("staging_dir", "evidence_staging")
        json_path = staging_dir / "release-evidence.json"
        markdown_path = staging_dir / "release-evidence.md"
        _atomic_write_text(json_path, json_text)
        _atomic_write_text(markdown_path, markdown_text)
        os.rename(str(staging_dir), str(evidence_dir))
        if resource_ledger is not None:
            resource_ledger.release("staging_dir", "evidence_staging")
    except BaseException as original_exc:
        # Task 15 Task 11 corrective v3, Section 21 (hardened by v4,
        # Section 28): staging cleanup on a failed publication is
        # itself a release-owned cleanup step, not best-effort -- if it
        # ALSO fails, that must still surface to the caller rather than
        # being silently swallowed, and distinguishably from a plain
        # publication failure (residue was left on disk). `original_exc`
        # is bound explicitly and chained via `from` -- Python's
        # automatic `__context__` tracking follows whichever exception
        # is innermost-active, which by this point would be the
        # cleanup's own OSError, not the original publication failure,
        # so relying on implicit chaining alone would lose it. Setting
        # `__cause__` to `original_exc` explicitly is what actually
        # preserves it here.
        try:
            shutil.rmtree(staging_dir)
        except FileNotFoundError:
            pass
        # Task 15 Task 11 corrective v6, Section 10: audited cleanup
        # covers ordinary Exception generically -- not a hard-coded
        # tuple of specific classes -- so an unanticipated exception
        # class raised while removing the staging directory can never
        # escape uncaught instead of becoming a recorded
        # StagingCleanupError. Never `except BaseException`.
        except Exception:  # noqa: BLE001 -- re-raised as StagingCleanupError immediately below, never dropped
            raise StagingCleanupError(
                "evidence staging directory cleanup failed after a publication failure"
            ) from original_exc
        else:
            # Task 15 Task 11 corrective v9, Section 20: verified
            # successful removal (or it was never created at all, the
            # FileNotFoundError case above, in which case this is
            # always a safe no-op) -- release now. Never released on
            # the StagingCleanupError branch above, where disposal was
            # NOT verified and genuine residue remains.
            if resource_ledger is not None:
                resource_ledger.release("staging_dir", "evidence_staging")
        raise

    return evidence_dir / "release-evidence.json", evidence_dir / "release-evidence.md"


def _scenario_field(result, key: str, default=None):
    # Task 15 Task 11 corrective v9, Section 5: `result` is normally a
    # deep-frozen `types.MappingProxyType` (evidence.scenarios[name]),
    # not a plain `dict` -- `isinstance(x, Mapping)` (the ABC) is
    # required so this still reads correctly, alongside the
    # attribute-based fallback for a raw dataclass/object.
    if isinstance(result, Mapping):
        return result.get(key, default)
    return getattr(result, key, default)


def _scenario_lines(scenarios: dict) -> list:
    """Task 15 Task 11 corrective v3, Section 25: renders every
    scenario's DECISION-BEARING typed assertions, not just its status/
    duration -- a human reading only the Markdown must be able to see
    WHY a scenario counted as proof of its release invariant."""
    lines = ["## Scenarios", ""]
    if not scenarios:
        lines.append("_none recorded_")
    for name, result in scenarios.items():
        status = _scenario_field(result, "status", "?")
        duration_ms = _scenario_field(result, "duration_ms")
        suffix = f" ({duration_ms:.0f} ms)" if isinstance(duration_ms, (int, float)) else ""
        lines.append(f"- **{name}**: {status}{suffix}")
        assertions = _scenario_field(result, "assertions") or {}
        for key, value in assertions.items():
            lines.append(f"  - {key}: {value}")
    lines.append("")
    return lines


def _dict_lines(heading: str, data: dict) -> list:
    lines = [heading, ""]
    if not data:
        lines.append("_none recorded_")
    for key, value in data.items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    return lines


def _failure_record_lines(heading: str, record: dict | None) -> list:
    if not record:
        return []
    return [
        heading,
        "",
        f"- category: {record.get('category')}",
        f"- stage: {record.get('stage')}",
        f"- code: {record.get('code')}",
        f"- safe_message: {record.get('safe_message')}",
        "",
    ]


def render_json_evidence(evidence: ReleaseEvidence, secrets: SecretRegistry) -> str:
    doc = secrets.redact_value(evidence.to_dict())
    text = json.dumps(doc, indent=2, sort_keys=True)
    secrets.assert_no_secrets_present(text)
    return text


def render_markdown_evidence(evidence: ReleaseEvidence, secrets: SecretRegistry) -> str:
    run = evidence.run
    authority = run.get("authority") or {}
    lines = [
        f"# Phase 1 Release Evidence — {run.get('run_id')}",
        "",
        f"- schema_version: {evidence.schema_version}",
        f"- git_sha: `{run.get('git_sha')}`",
        f"- started_at: {run.get('started_at')}",
        f"- finished_at: {run.get('finished_at')}",
        f"- overall: **{evidence.overall}**",
        f"- exit_code: {evidence.exit_code}",
        f"- cleanup_status: {evidence.cleanup_status}",
        "",
        *_dict_lines("## Authority", authority),
        *_dict_lines("## Environment", evidence.environment),
        *_dict_lines("## Profiles", evidence.profiles),
        *_dict_lines("## Validation", evidence.validation),
        *_scenario_lines(evidence.scenarios),
        *_dict_lines("## Proofs", evidence.proofs),
        *_dict_lines("## Retention", evidence.retention),
        *_failure_record_lines("## Primary failure", evidence.primary_failure),
        *_failure_record_lines("## Cleanup failure", evidence.cleanup_failure),
        *_failure_record_lines("## Finalization failure", evidence.finalization_failure),
    ]

    text = secrets.redact_text("\n".join(lines))
    secrets.assert_no_secrets_present(text)
    return text


def write_markdown_evidence(path, evidence: ReleaseEvidence, secrets: SecretRegistry) -> None:
    text = render_markdown_evidence(evidence, secrets)
    _atomic_write_text(path, text)
