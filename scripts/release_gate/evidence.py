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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

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
    """Task 15 Task 11 corrective: the ONE canonical in-memory evidence
    record (EvidenceV1). Both write_json_evidence() and
    write_markdown_evidence() render from this same object -- never two
    independently-maintained partial schemas."""

    schema_version: int
    run: dict  # run_id, git_sha, started_at, finished_at, platform, central_profile, node_profile
    validation: dict  # clean_tree, central_check, node_check, central_migration_drift, node_migration_drift
    scenarios: dict  # scenario name -> ScenarioResult fields (as dict)
    proofs: dict  # package_sha256, payload_sha256, public_key_fingerprint, signature_verified, outbox_to_inbox
    retained_resources: dict  # scenario name -> {central_database, node_database, workspace_path}, keep-on-failure only
    primary_failure: dict | None  # FailureRecord.to_dict(), or None on PASS
    cleanup_failure: dict | None  # FailureRecord.to_dict(), or None if cleanup fully succeeded
    cleanup_status: str  # e.g. "clean" | "retained_for_debug" | "failed"
    overall: str
    exit_code: int

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run": self.run,
            "validation": self.validation,
            "scenarios": self.scenarios,
            "proofs": self.proofs,
            "retained_resources": self.retained_resources,
            "primary_failure": self.primary_failure,
            "cleanup_failure": self.cleanup_failure,
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
    doc = secrets.redact_value(evidence.to_dict())
    text = json.dumps(doc, indent=2, sort_keys=True)
    secrets.assert_no_secrets_present(text)
    _atomic_write_text(path, text)


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


def write_markdown_evidence(path, evidence: ReleaseEvidence, secrets: SecretRegistry) -> None:
    run = evidence.run
    lines = [
        f"# Phase 1 Release Evidence — {run.get('run_id')}",
        "",
        f"- schema_version: {evidence.schema_version}",
        f"- git_sha: `{run.get('git_sha')}`",
        f"- started_at: {run.get('started_at')}",
        f"- finished_at: {run.get('finished_at')}",
        f"- platform: {run.get('platform')}",
        f"- central_profile: {run.get('central_profile')}",
        f"- node_profile: {run.get('node_profile')}",
        f"- overall: **{evidence.overall}**",
        f"- exit_code: {evidence.exit_code}",
        f"- cleanup_status: {evidence.cleanup_status}",
        "",
        *_dict_lines("## Validation", evidence.validation),
        *_scenario_lines(evidence.scenarios),
        *_dict_lines("## Proofs", evidence.proofs),
        *_dict_lines("## Retained resources", evidence.retained_resources),
        *_failure_record_lines("## Primary failure", evidence.primary_failure),
        *_failure_record_lines("## Cleanup failure", evidence.cleanup_failure),
    ]

    text = secrets.redact_text("\n".join(lines))
    secrets.assert_no_secrets_present(text)
    _atomic_write_text(path, text)
