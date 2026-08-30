# Task 14 Corrective Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the eight Important findings from the independent Task 14 architecture/security review without expanding Task 14 scope, and record the newly approved singleton-413 policy as T14-D40.

**Architecture:** Preserve the accepted Node → Central transactional Outbox/Inbox design. Tighten deployment-profile authority, immutable-write boundaries, lease fencing, wire-size/protocol recovery, strict event validation, timestamp normalization, conservative ACK parsing, and credential secrecy. Add exactly one new architecture decision: batch 413 is split/adapted; a singleton event that still receives 413 becomes `DEAD_LETTER(EVENT_TOO_LARGE)`.

**Tech Stack:** Python 3.12, Django 5.2, PostgreSQL 18, pytest, Django password hashers, Python standard-library HTTP/JSON utilities.

**Spec:** `docs/superpowers/specs/2026-08-30-task14-node-central-event-sync-design.md`

## Global Constraints

- Start from reviewed Task 14 HEAD `232e70b3935c72fdccd62075546c1483aa2b860d`.
- Independent review verdict: **B. TASK 14 CONDITIONAL**.
- Fix only the eight Important findings from the independent review.
- Minor findings remain backlog for this corrective unless a change is mechanically necessary to fix an Important finding.
- Preserve T14-D01 through T14-D39.
- Add exactly one new approved decision: **T14-D40 — deterministic 413 recovery**.
- T14-D40 semantics: batch size > 1 and HTTP 413 → split/adapt, retry smaller batches; singleton still HTTP 413 → `DEAD_LETTER` with permanent code `EVENT_TOO_LARGE`; good events behind/beside the offending event must continue; no new Outbox state such as `BLOCKED`; do not treat arbitrary 400 as event-too-large; deterministic 400 must not blindly rebuild the identical blocking batch forever.
- Do not redesign Task 14. Do not start Task 15.
- No Central → Node event sync. No package auto-download. No DB replication. No Central learning projection. No Celery/Redis/RabbitMQ/Kafka. No self-service enrollment.
- Keep `FileSyncTransport` dev/test/reference-only. Keep at-least-once transport and durable Inbox ACK semantics.
- All corrective work is TDD: RED → minimal fix → GREEN.
- Run real PostgreSQL concurrency tests for lease fencing and duplicate races.

## Corrective scope mapping

The independent review identified exactly these Important findings:

1. Central-only authority is exposed on the Node profile.
2. Shared immutable-row writer accepts caller-controlled authorization.
3. Lease recovery lacks stale-worker fencing.
4. 400/413 handling can retry deterministic failures forever; byte accounting mismatches actual request bytes.
5. Complete event-envelope validation is insufficient; poison events can reach DB exceptions.
6. Timestamp normalization breaks exact replay idempotency.
7. Duplicate/incomplete ACK entries can authorize delivery/dead-letter.
8. Plaintext Node credential appears in repr.

No other architecture changes are approved.

---

### Task 1: Record T14-D40 and corrective review scope

**Files:** Modify `docs/devlog/2026-08-30-task14-sync-decisions.md`, `docs/superpowers/specs/2026-08-30-task14-node-central-event-sync-design.md`.

- [x] Add T14-D40 to the decision log, verbatim as specified.
- [x] Update the design spec's error/transport/batching sections to reflect byte accounting, 413 split/adaptation, singleton EVENT_TOO_LARGE, deterministic-400 non-tight-loop behavior, and good-event progress.
- [x] Self-review against T14-D09, T14-D16, T14-D29, T14-D30, T14-D39 for contradictions.
- [x] Commit docs-only: `docs: record Task 14 protocol corrective`.

### Task 2: Enforce Central/Node deployment capabilities

**Files:** Modify `linuxlab/sync/api.py`, `linuxlab/sync/management/commands/manage_sync_node.py`, `linuxlab/sync/management/commands/sync_events.py`; add focused profile tests.

- [ ] RED profile-boundary tests using the actual project profile mechanism.
- [ ] One fail-closed deployment capability helper (Central-only sync authority vs Node-only outbound worker), reused by HTTP and commands.
- [ ] GREEN plus `manage.py check` on both real profile settings.
- [ ] Commit: `fix: enforce Task 14 deployment boundaries`.

### Task 3: Seal immutable trust-record writers and hide credential repr

**Files:** Modify `linuxlab/sync/models.py`, `linuxlab/sync/authentication.py`, Task 14 model/auth tests.

- [ ] RED trust-boundary probes: attempt to rewrite every protected immutable field on `NodeRegistration`/`OutboxMessage`/`InboxMessage` via the shared writer, including passing a field name as a caller-supplied allow-list under the old signature.
- [ ] RED credential repr tests.
- [ ] Implement model-fixed mutable field maps (or model-specific private writers); remove caller-controlled `allowed_fields`.
- [ ] `IssuedNodeCredential.secret` excluded from `repr()`.
- [ ] GREEN.
- [ ] Commit: `fix: seal sync trust record writers`.

### Task 4: Fence lease ownership against stale workers

**Files:** Modify `linuxlab/sync/outbox.py`, `linuxlab/sync/contracts.py` (if needed), `linuxlab/sync/models.py`, migrations, outbox concurrency tests.

- [ ] RED two-worker stale-claim tests with separate PostgreSQL connections (delivered-then-stale-transient; released-then-stale-success).
- [ ] Explicit claim-generation fencing (`claim_token`), additive migration if needed.
- [ ] Every delivery/release write filters by event_id + state=IN_FLIGHT + exact claim_token; a stale write affects zero rows.
- [ ] GREEN including real concurrency test.
- [ ] Commit: `fix: fence stale outbox workers`.

### Task 5: Use one real wire serializer for byte accounting and implement T14-D40

**Files:** Modify `linuxlab/sync/contracts.py`, `linuxlab/sync/transports.py`, `linuxlab/sync/outbox.py`; transport/flush/outbox tests.

- [ ] RED exact-byte-boundary tests via one shared `serialize_sync_batch()`.
- [ ] RED 413 split test (batch > 1).
- [ ] RED singleton-413 test (T14-D40).
- [ ] RED deterministic-400 test (no tight loop, no false delivery/dead-letter).
- [ ] Implement shared serialization, split/adapt orchestration, safe deterministic-400 handling.
- [ ] GREEN.
- [ ] Commit: `fix: recover deterministic sync protocol failures`.

### Task 6: Strictly validate and normalize the complete inbound event envelope

**Files:** Modify `linuxlab/sync/contracts.py`, `linuxlab/sync/registry.py`, `linuxlab/sync/ingestion.py`, `linuxlab/sync/api.py` (if needed); ingestion/API/idempotency tests.

- [ ] RED full-envelope validation tests (bounded aggregate_type, expected type, UUID fields, aggregate/payload consistency, schema version, timestamp).
- [ ] RED timestamp replay tests (UTC normalization; naive rejected).
- [ ] RED DB-poison isolation test.
- [ ] Implement `normalize_wire_event()` boundary; narrow `IntegrityError` race handling to confirmed event_id match only.
- [ ] GREEN.
- [ ] Commit: `fix: normalize and validate sync envelopes`.

### Task 7: Fail closed on ambiguous ACKs

**Files:** Modify `linuxlab/sync/transports.py`, `linuxlab/sync/contracts.py` (if needed), `linuxlab/sync/outbox.py`; transport/flush ACK tests.

- [ ] RED ACK ambiguity matrix (duplicate IDs, conflicting verdicts, missing rejection code, unknown IDs, malformed mixtures).
- [ ] Implement strict parser: no last-wins dict; ambiguity never authorizes delivery/dead-letter.
- [ ] GREEN.
- [ ] Commit: `fix: reject ambiguous sync acknowledgements`.

### Task 8: Corrective end-to-end verification and dev log

**Files:** Modify `linuxlab/sync/tests/test_task14_e2e.py`, `docs/devlog/2026-08-30-task14-sync-decisions.md`.

- [ ] E2E profile proof, stale-worker proof, 413 progress proof, poison-neighbor proof, ACK-lost-with-non-UTC-timestamp proof, ambiguous-ACK proof.
- [ ] Run full Task 14 suite, full regression suite, ruff, both profiles' `check`/`makemigrations --check --dry-run`.
- [ ] Append corrective implementation record to the dev log.
- [ ] Commit: `docs: record Task 14 corrective implementation`.
- [ ] STOP. Do not start Task 15.
