# Task 14 Second Corrective Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three remaining Important findings from the focused Task 14 corrective re-review without reopening already-closed findings or expanding Task 14 scope.

**Architecture:** Preserve the accepted Task 14 Node → Central Outbox/Inbox architecture and T14-D01–D40. Seal the immutable-write policy with an immutable internal mapping, isolate deterministic HTTP 400 batches by splitting them while applying protocol-specific long backoff to singleton 400 events, and make ACK parsing fail closed on any repeated parseable event ID before malformed entries are discarded.

**Tech Stack:** Python 3.12, Django 5.2, PostgreSQL 18, pytest, Python standard library.

**Spec:** `docs/superpowers/specs/2026-08-30-task14-node-central-event-sync-design.md`

## Global Constraints

- Start from corrective reviewed HEAD `c4517e2d6a0701c93f222d96bdcc27d96aceb440`.
- Previous focused re-review verdict: **B. TASK 14 STILL CONDITIONAL**.
- Exactly three Important findings remain open:
  1. immutable-write policy remains runtime-widenable;
  2. deterministic HTTP 400 can reconstruct the same starving multi-event batch;
  3. malformed duplicate ACK can authorize delivery.
- The following are already CLOSED and must not be redesigned: profile authority; lease fencing; 413 split/singleton `EVENT_TOO_LARGE`; envelope validation/poison isolation; timestamp normalization/idempotency; credential repr.
- Preserve T14-D01 through T14-D40.
- Add exactly one human-approved decision: **T14-D41 — deterministic HTTP 400 isolation**.
- T14-D41: multi-event HTTP 400 → bisect/split to isolate the fault and allow good-neighbor progress; singleton HTTP 400 → remain retryable `PENDING`, no `DEAD_LETTER`; singleton 400 uses protocol-specific long backoff and remains operator-visible; worker must continue processing other due events rather than blocking behind the singleton; no new Outbox state; do not reinterpret request-level 400 as event-level `INVALID_PAYLOAD`.
- Do not invent T14-D42.
- Keep T14-D40 unchanged: multi-event 413 → split/adapt; singleton 413 → `DEAD_LETTER(EVENT_TOO_LARGE)`.
- Keep at-least-once semantics, durable Inbox ACK, claim fencing, and conservative irreversible transitions.
- Do not start Task 15.
- Do not fix unrelated Minor backlog: dead backoff settings; FileSyncTransport determinism; durable idempotency-conflict audit; claim-path index / DB schema-version hardening.
- All code changes use RED → GREEN TDD.

---

### Task 1: Record T14-D41 and second corrective scope

**Files:** Modify `docs/devlog/2026-08-30-task14-sync-decisions.md`, `docs/superpowers/specs/2026-08-30-task14-node-central-event-sync-design.md`.

- [x] Add T14-D41 to the decision log, verbatim as specified.
- [x] Update the design spec's transport/error-handling sections: 400 multi-event isolation by split/bisection; singleton 400 remains PENDING with long protocol backoff; no event-level permanent rejection inferred from request-level 400; worker progress for unrelated events; distinction from singleton 413.
- [x] Self-review against T14-D09, T14-D30, T14-D40; confirm no T14-D42.
- [x] Commit docs: `docs: record Task 14 HTTP 400 isolation`.

### Task 2: Make immutable-write policy non-widenable

**Files:** Modify `linuxlab/sync/models.py`, Task 14 immutable-model tests.

- [ ] RED policy-mutation tests: attempt `_MUTABLE_FIELDS_BY_MODEL[OutboxMessage] = frozenset({"payload"})` and `_MUTABLE_FIELDS_BY_MODEL[OutboxMessage].add("payload")` (or equivalent) against all three models; expect `TypeError`/`AttributeError`, policy unchanged, subsequent tampered write still fails, row unchanged.
- [ ] Replace the mutable dict with `types.MappingProxyType` over `frozenset` values; writers consult only this internal mapping; no public mutation API.
- [ ] Verify legitimate writers (rotation/revoke, Outbox delivery/claim/release metadata, Inbox processing metadata) still work.
- [ ] GREEN: `test_task14_models.py`, `test_task14_authentication.py`, `test_task14_outbox.py`, `test_task14_processing.py`.
- [ ] Commit: `fix: freeze sync mutable-field policy`.

### Task 3: Implement deterministic HTTP 400 isolation (T14-D41)

**Files:** Modify `linuxlab/sync/outbox.py`, `linuxlab/sync/contracts.py` (only if needed), `linuxlab/sync/management/commands/sync_events.py`; modify/create `test_task14_flush.py`, `test_task14_command.py`, relevant transport tests.

- [ ] RED multi-event 400 isolation test: scripted transport `[A,B,C,D]→400`, `[A,B]→400`, `[A]→accepted`, `[B]→400`, `[C,D]→accepted`; expect A,C,D DELIVERED, B PENDING with protocol-specific long backoff, no DEAD_LETTER from 400, original batch not blindly resent, all transitions claim-token fenced.
- [ ] RED real worker-delay regression: two-event batch gets deterministic 400, advance clock beyond the old fixed interval, assert the identical two-event batch is not resent unchanged, a valid neighbor progresses independently. Use injected clock/transport fixtures, not real sleeps.
- [ ] RED singleton 400 semantics: PENDING, no `EVENT_TOO_LARGE`, no DEAD_LETTER, `next_attempt_at` reflects long protocol backoff, flush/worker reports protocol fault, a later unrelated event remains claimable.
- [ ] Implement a shared split helper for protocol faults, reusing the safe 413 subset-send mechanism where practical but keeping 413-singleton (`DEAD_LETTER(EVENT_TOO_LARGE)`) and 400-singleton (`PENDING` + long backoff) terminal semantics distinct. No DB transaction around HTTP.
- [ ] Ensure the worker does not block the queue behind a degraded protocol condition without rebuilding the identical blocking batch.
- [ ] GREEN: `test_task14_flush.py`, `test_task14_command.py`, `test_task14_transports.py`.
- [ ] Commit: `fix: isolate deterministic HTTP 400 batches`.

### Task 4: Detect raw duplicate ACK IDs before filtering malformed entries

**Files:** Modify `linuxlab/sync/contracts.py`, `linuxlab/sync/transports.py` (if needed); modify/create `test_task14_transports.py`, `test_task14_flush.py`.

- [ ] RED raw-duplicate ACK matrix: `accepted + rejected-without-code` (both orders), `accepted + unknown-status` (both orders), `duplicate + malformed same ID` (both orders), plus existing cases (accepted+rejected, repeated accepted, unknown ACK ID, missing sent ID). Expected: any repeated parseable sent event ID → no irreversible action, stays unconfirmed/retryable.
- [ ] Harden the parser in two phases: (1) walk raw results, extract parseable event_id where possible, count every occurrence BEFORE validating status/code; (2) a parseable event_id occurring more than once is marked ambiguous and emits no actionable ACK for it — only unique IDs proceed to status/code validation. No last-entry-wins dictionary.
- [ ] Verify via `flush_outbox()`: ambiguous ID stays PENDING/retryable, never DELIVERED/DEAD_LETTER; valid unique neighbors still progress.
- [ ] GREEN.
- [ ] Commit: `fix: fail closed on malformed duplicate ACKs`.

### Task 5: Second corrective closure verification

**Files:** Modify `docs/devlog/2026-08-30-task14-sync-decisions.md`.

- [ ] Re-run all three original failure probes (immutable policy, 400 starvation, malformed duplicate ACK).
- [ ] Re-run previously CLOSED regression areas: profile authority, claim-token fencing, T14-D40 413, envelope validation, UTC idempotency, credential repr.
- [ ] Run full sync suite, full project suite, ruff, both profiles' `check`/`makemigrations --check --dry-run`.
- [ ] Append second corrective implementation record to the dev log.
- [ ] Commit: `docs: record Task 14 second corrective`.
- [ ] STOP. Do not start Task 15.
