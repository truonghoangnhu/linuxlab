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


class SecretRegistry:
    """Accumulates known secret VALUES seen during a gate run so
    redact_text()/redact_value() can scrub every occurrence of each
    exact value from evidence/log output, wherever it appears. This is
    defense in depth, never a substitute for not putting secrets in
    evidence in the first place."""

    def __init__(self):
        self._secrets: set[str] = set()

    def register(self, value) -> None:
        if value:
            self._secrets.add(str(value))

    def redact_text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        return text

    def redact_value(self, value):
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {key: self.redact_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_value(item) for item in value]
        return value


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    status: str  # "PASS" | "FAIL"
    assertions: dict = field(default_factory=dict)
    duration_ms: float = 0.0
    detail: str = ""


@dataclass
class ReleaseEvidence:
    schema_version: int
    run_id: str
    git_sha: str
    started_at: str
    finished_at: str
    environment: dict
    checks: dict
    scenarios: dict
    artifacts: dict
    overall: str
    cleanup_failure: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "git_sha": self.git_sha,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "environment": self.environment,
            "checks": self.checks,
            "scenarios": self.scenarios,
            "artifacts": self.artifacts,
            "overall": self.overall,
            "cleanup_failure": self.cleanup_failure,
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
    _atomic_write_text(path, text)


def _scenario_lines(scenarios: dict) -> list:
    lines = ["## Scenarios", ""]
    if not scenarios:
        lines.append("_none recorded_")
    for name, result in scenarios.items():
        status = result.get("status", "?") if isinstance(result, dict) else getattr(
            result, "status", "?"
        )
        lines.append(f"- **{name}**: {status}")
    lines.append("")
    return lines


def write_markdown_evidence(path, evidence: ReleaseEvidence, secrets: SecretRegistry) -> None:
    lines = [
        f"# Phase 1 Release Evidence — {evidence.run_id}",
        "",
        f"- schema_version: {evidence.schema_version}",
        f"- git_sha: `{evidence.git_sha}`",
        f"- started_at: {evidence.started_at}",
        f"- finished_at: {evidence.finished_at}",
        f"- overall: **{evidence.overall}**",
        "",
        "## Environment",
        "",
        *(f"- {key}: {value}" for key, value in evidence.environment.items()),
        "",
        *_scenario_lines(evidence.scenarios),
        "## Checks",
        "",
        *(f"- {key}: {value}" for key, value in evidence.checks.items()),
        "",
    ]
    if evidence.cleanup_failure:
        lines += ["## Cleanup failure", "", str(evidence.cleanup_failure), ""]

    text = secrets.redact_text("\n".join(lines))
    _atomic_write_text(path, text)
