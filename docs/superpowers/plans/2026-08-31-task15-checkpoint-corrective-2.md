# Task 15 Checkpoint Corrective Plan 2 — Two Remaining Critical Findings

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the final two Critical checkpoint findings so Tasks 1–6 become a safe foundation for Task 15 Tasks 7–15.

**Architecture:** Keep all approved Task 15 decisions T15-D01–D26 unchanged. Harden the release-gate ownership handles so destructive PostgreSQL operations resolve only canonical names belonging to the validated current run, and make readiness derive exclusively from an authentic managed Gunicorn handle produced by the process launcher. No new architecture decision is required.

**Spec:** `docs/superpowers/specs/2026-08-31-task15-phase1-release-readiness-gate-design.md`

## Global Constraints

- Start from corrective HEAD `2c341bb5ab17e3f42846a5bfbbc939c2472cc1cb`.
- Do not start Task 15 Tasks 7–15.
- Fix only: (1) Critical — forged `GateDatabaseNames` can still target non-current-run databases; (2) Critical — `wait_ready()` can still false-PASS when given a fake endpoint plus an unrelated live child.
- Findings already CLOSED and not to be redesigned: evidence key/value/overlap secret redaction; Node readiness configuration; backoff validation/T14-D41 preservation.
- Preserve T15-D01–D26. Do not add T15-D27.
- Strict RED → GREEN TDD.
- Use real PostgreSQL and real Gunicorn adversarial probes before checkpoint re-review.

---

### Task 1: Make database ownership canonical and non-forgeable through the public helper boundary
### Task 2: Make readiness accept only an authentic launcher-produced Gunicorn handle
### Task 3: Final checkpoint verification

Full task-by-task step detail (exact RED test scenarios, preferred implementation shapes, commit sequencing) is authoritative in the human-supplied corrective plan delivered with this task and is followed exactly during implementation; this repository copy satisfies the "commit the plan" convention established for prior Task 14/15 correctives.
