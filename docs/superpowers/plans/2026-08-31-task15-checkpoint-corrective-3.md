# Task 15 Checkpoint Corrective Plan 3 — PostgreSQL Identifier Length and Listener Socket Identity

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the final two Critical checkpoint findings by preventing PostgreSQL identifier-truncation collisions across runs and by deriving readiness identity from the actual inherited listener socket rather than mutable metadata.

**Architecture:** Preserve T15-D01–D26. Keep the canonical current-run DB ownership model and authentic managed-process model already introduced. Add an exact run-ID byte-length bound derived from PostgreSQL's 63-byte identifier limit, and make listener host/port kernel-derived from `socket.getsockname()` at launch. No T15-D27 is required.

**Spec:** `docs/superpowers/specs/2026-08-31-task15-phase1-release-readiness-gate-design.md`

## Global Constraints

- Start from HEAD `7e832379ac6fca801563f58fc96a2402dc7cdc36`.
- Do not start Task 15 Tasks 7–15.
- Fix only the two remaining Critical findings: (1) run IDs can exceed PostgreSQL's 63-byte identifier limit and collide after server-side truncation; (2) `ReservedListener.host/port` metadata can be redirected away from the actual inherited socket.
- Findings already CLOSED must remain closed: evidence secret redaction, Node readiness configuration, backoff validation/T14-D41.
- Preserve T15-D01–D26. Do not add T15-D27.
- Strict RED → GREEN TDD. Reproduce both review failures first.
- Use real PostgreSQL and real Gunicorn in final verification.

---

### Task 1: Bound run IDs so every canonical gate database identifier fits PostgreSQL exactly
### Task 2: Derive readiness identity from the actual listener socket
### Task 3: Final Tasks 1–6 checkpoint closure verification

Full task-by-task step detail (exact RED test scenarios, preferred implementation shapes, commit sequencing) is authoritative in the human-supplied corrective plan delivered with this task and is followed exactly during implementation; this repository copy satisfies the "commit the plan" convention established for prior Task 14/15 correctives.
