# Task 15 Checkpoint Corrective Plan — Tasks 1–6

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the five checkpoint blockers found in Tasks 1–6 before any Task 15 Tasks 7–15 work proceeds.

**Architecture:** Preserve the approved Task 15 design and all T15-D01–D26 decisions. Apply only the minimum corrective changes to database ownership safety, Gunicorn readiness identity, release-evidence secret redaction, Node readiness configuration validation, and sync backoff validation. Do not add T15-D27 unless a genuinely new architecture decision is unavoidable.

**Spec:** `docs/superpowers/specs/2026-08-31-task15-phase1-release-readiness-gate-design.md`

## Global Constraints

- Start from checkpoint HEAD `ad8476b18e6c63f873700952ebcde34a4b8d1d3f`.
- Do not start Task 15 Tasks 7–15.
- Fix exactly the five review blockers: (1) Critical — DB operations can target non-run-owned databases; (2) Critical — readiness can false-PASS against the wrong service; (3) Critical — registered secrets can survive JSON evidence as dictionary keys; (4) Important — Node readiness ignores required sync configuration; (5) Important — pathological backoff settings can violate T14-D41.
- Preserve T15-D01–D26. Do not introduce T15-D27 unless truly unavoidable.
- No unrelated refactor. Strict RED → GREEN TDD.
- Use real PostgreSQL and real Gunicorn probes for the two Critical lifecycle findings.
- Re-run the full 904-test baseline plus corrective tests before requesting re-review.

---

### Task 1: Bind database operations to the exact current run
### Task 2: Bind readiness to the exact managed Gunicorn child
### Task 3: Make evidence redaction cover keys and fail closed
### Task 4: Make Node readiness validate active-profile requirements
### Task 5: Validate sync backoff settings while preserving T14-D41
### Task 6: Checkpoint closure verification

Full task-by-task step detail (exact RED test scenarios, preferred implementation shapes, commit sequencing) is authoritative in the human-supplied corrective plan delivered with this task and is followed exactly during implementation; this repository copy satisfies the "commit the plan" convention established for prior Task 14/15 correctives.
