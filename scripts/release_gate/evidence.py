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
import uuid as uuid_module
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_VALID_DELIVERY_STATES = ("PENDING", "DELIVERED")


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
    public_key_fingerprint."""
    if not isinstance(value, str) or not _SHA256_HEX_RE.match(value):
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
        if not isinstance(self.node_state, str) or self.node_state not in _VALID_DELIVERY_STATES:
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


def _validate_bool_assertion(value, key: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidProofError(f"assertion {key!r} must be a bool")
    return value


def _validate_nonneg_int_assertion(value, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidProofError(f"assertion {key!r} must be a non-negative int")
    return value


def _validate_delivery_state_assertion(value, key: str) -> str:
    if not isinstance(value, str) or value not in _VALID_DELIVERY_STATES:
        raise InvalidProofError(f"assertion {key!r} must be one of {_VALID_DELIVERY_STATES}")
    return value


# Section 8's closed assertion-key registry, covering the N1/N3 example
# sets given in the corrective spec. A key absent here is simply not
# release-evidence content (silently dropped, same as an unrecognized
# proof field); a key present here with a value that fails its own
# validator raises InvalidProofError rather than admitting an
# unconstrained arbitrary value.
_ASSERTION_VALIDATORS = {
    "existing_active_preserved": _validate_bool_assertion,
    "active_package_identity_unchanged": _validate_bool_assertion,
    "replacement_rejected": _validate_bool_assertion,
    "central_commit_before_ack_loss": _validate_bool_assertion,
    "node_pending_before_retry": _validate_bool_assertion,
    "final_inbox_count": _validate_nonneg_int_assertion,
    "final_node_state": _validate_delivery_state_assertion,
    "duplicate_idempotent": _validate_bool_assertion,
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
            value = detail["outbox_to_inbox"]
            if not isinstance(value, dict):
                raise InvalidProofError(
                    "outbox_to_inbox must be a typed {event_uuid, inbox_count, node_state, "
                    "duplicate_idempotent} mapping, not a bare string"
                )
            proofs["outbox_to_inbox"] = OutboxInboxProof(**value).to_dict()
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

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "stage": self.stage,
            "code": self.code,
            "safe_message": self.safe_message,
        }


@dataclass
class ReleaseEvidence:
    """Task 15 Task 11 corrective v2: the ONE canonical in-memory
    evidence record (EvidenceV1, schema_version 1). Both
    write_json_evidence() and write_markdown_evidence() render from
    this same object -- never two independently-maintained partial
    schemas.

    `retention` replaces the prior `retained_resources` field
    (corrective v2, Section 3): execution resources (databases,
    workspace, `.llpkg`, keys, dumps, logs) are NEVER retained
    regardless of --keep-on-failure or failure stage --
    `execution_resources_retained` is therefore always False. Only the
    sanitized evidence pair itself may ever be described as retained
    (`sanitized_debug_bundle_retained`)."""

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

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run": self.run,
            "environment": self.environment,
            "profiles": self.profiles,
            "validation": self.validation,
            "scenarios": self.scenarios,
            "proofs": self.proofs,
            "retention": self.retention,
            "primary_failure": self.primary_failure,
            "cleanup_failure": self.cleanup_failure,
            "finalization_failure": self.finalization_failure,
            "cleanup_status": self.cleanup_status,
            "overall": self.overall,
            "exit_code": self.exit_code,
        }


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


def publish_evidence(evidence_dir, evidence: ReleaseEvidence, secrets: SecretRegistry) -> tuple[Path, Path]:
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
    never observable at the final location."""
    evidence_dir = Path(evidence_dir)
    if evidence_dir.exists():
        raise EvidencePublicationError(f"release evidence directory already exists: {evidence_dir}")

    json_text = render_json_evidence(evidence, secrets)
    markdown_text = render_markdown_evidence(evidence, secrets)

    evidence_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = evidence_dir.parent / f".{evidence_dir.name}.staging-{os.getpid()}-{uuid_module.uuid4().hex[:8]}"
    try:
        staging_dir.mkdir(parents=True, exist_ok=False)
        json_path = staging_dir / "release-evidence.json"
        markdown_path = staging_dir / "release-evidence.md"
        _atomic_write_text(json_path, json_text)
        _atomic_write_text(markdown_path, markdown_text)
        os.rename(str(staging_dir), str(evidence_dir))
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return evidence_dir / "release-evidence.json", evidence_dir / "release-evidence.md"


def _scenario_lines(scenarios: dict) -> list:
    lines = ["## Scenarios", ""]
    if not scenarios:
        lines.append("_none recorded_")
    for name, result in scenarios.items():
        status = result.get("status", "?") if isinstance(result, dict) else getattr(
            result, "status", "?"
        )
        duration_ms = result.get("duration_ms") if isinstance(result, dict) else getattr(
            result, "duration_ms", None
        )
        suffix = f" ({duration_ms:.0f} ms)" if isinstance(duration_ms, (int, float)) else ""
        lines.append(f"- **{name}**: {status}{suffix}")
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
