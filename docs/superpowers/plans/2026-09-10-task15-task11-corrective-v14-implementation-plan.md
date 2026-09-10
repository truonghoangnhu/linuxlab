# Task 15 Task 11 Corrective v14 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every valid Critical and Important boundary defect from the independent v13 review while preserving all v7-v13 release-gate invariants and leaving Task 15 Task 12 untouched.

**Architecture:** Strengthen the existing release-gate boundaries in place. `GateRunResult` becomes a relational coherence gate; proof validation and terminal/authority dataclasses use exact canonical types; `ResourceLedger` uses stable IDs as the only structural authority and makes relocation atomic without reparenting; freeze/thaw/export become symmetric fail-closed operations. No new state-machine, serializer framework, ResourceGraph subsystem, schema, CLI bypass, or Task 12 work is introduced.

**Tech Stack:** Python 3.12.14, Django 5.2, Wagtail 7.4, PostgreSQL 18.1, psycopg, Gunicorn, pytest/pytest-django, Ruff, dataclasses, `types.MappingProxyType`, `uuid.UUID`.

**Spec:** `docs/superpowers/specs/2026-09-10-task15-task11-corrective-v14-design.md`

## Global Constraints

- Code baseline before v14 production changes: `65c579a45e07d867a3cbabb69b1615e09bf376ea`.
- The approved spec and this implementation plan must be committed before production/test implementation commits begin.
- Work only on branch `feature/phase1-learning-foundation`.
- Keep `.env` ignored and never print `POSTGRES_PASSWORD`, bearer tokens, signing private keys, or other secrets.
- Before PostgreSQL-dependent tests: `set -a; source .env; set +a`.
- `POSTGRES_DB` is intentionally not required in `.env`; release-gate child processes pin the database explicitly.
- Existing exit contract remains exactly: `0=PASS`, `2=PRECHECK_FAILURE`, `3=STARTUP_FAILURE`, `4=SCENARIO_FAILURE`, `5=CLEANUP_FAILURE`.
- No migrations, DB-schema changes, `.llpkg` format changes, Task 14 protocol changes, Activity/Attempt changes, serializer framework, ResourceGraph subsystem, macOS CLI bypass, or publication-schema redesign.
- `parent_resource_id` is the sole structural ownership authority after acquisition. Locator containment must never decide traversal, release, retention, or PASS.
- Canonical boundaries validate; they do not coerce arbitrary values into validity.
- Every production change follows RED -> minimal GREEN -> focused regression -> commit.
- If any required fix cannot be made inside the spec's allowed areas, STOP and return to architectural review.
- Task 15 Task 12 remains STOP for the entire plan.

(See the design spec at the path above for the full nine-task breakdown: terminal-authority RED lock, GateRunResult coherence gate, proof/authority sealing, symmetric thaw/export, sole parent_resource_id reconciliation authority, atomic non-reparenting relocation, durable real parent-relocation lifecycle coverage, full corrective regression matrix, and exact-HEAD literal acceptance.)

## Implementation note

Per this repository's established convention for Task 15 Task 11 correctives (v9 through v13, each landed as exactly one "fix:" commit after full internal TDD/verification rather than one commit per plan-task), this plan is executed as a single internally-TDD'd implementation landing in one commit, with full verification run before that commit and confirmed again after it on the exact resulting HEAD.
