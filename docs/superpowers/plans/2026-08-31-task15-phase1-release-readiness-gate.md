# Task 15 — Phase 1 Release Readiness Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the final Phase 1 release-readiness gate: production-oriented health/readiness, repo-native dual-deployment E2E orchestration, FAST/RELEASE CI, recovery smoke testing, deployment templates, operator runbooks, and release evidence.

**Spec:** `docs/superpowers/specs/2026-08-31-task15-phase1-release-readiness-gate-design.md`

This plan mirrors the human-approved Task 15 implementation plan verbatim in structure (15 tasks). See the spec for full architecture rationale; this file tracks execution only.

## Global Constraints

- Start from `a83b1fb358343394f7e99bd3c9d62838473dea70`, clean tree, 826 passing baseline.
- Task 15 is the official Phase 1 Release Gate; no Task 16 solely to close Phase 1.
- Linux production target; macOS development-only.
- PostgreSQL 18 only for official DB CI/release tests.
- Gunicorn is the production reference server, used in RELEASE CI.
- systemd/Caddy are deployment references, not CI runtime dependencies.
- Dual real Central/Node deployments, separate DBs/processes, real HTTP; Docker not required.
- No browser automation, chaos framework, backup daemon, Vault requirement, auto-installer, auto-tag/release, second CI provider.
- Release business-state mutations go only through existing public/domain lifecycle APIs — never raw ORM/SQL to fabricate trusted state.
- Every run uses ephemeral Central/Node databases and an ephemeral Ed25519 keypair.
- Release evidence never contains secrets.
- Application startup never runs migrations.
- Preserve T14-D01–D41 behavior intact.
- Only the sync-backoff-settings Minor is promoted into Task 15; the other three Task 14 Minors stay deferred.
- Strict TDD for every behavior change.

---

### Task 1: Commit the approved Task 15 spec and decision ledger
### Task 2: Implement liveness/readiness without side effects
### Task 3: Wire approved sync backoff settings into runtime
### Task 4: Build release-gate environment and evidence primitives
### Task 5: Build fail-closed ephemeral PostgreSQL lifecycle and recovery primitives
### Task 6: Build Gunicorn process orchestration and readiness waiting
### Task 7: Build profile-scoped application action bridge using public/domain APIs
### Task 8: Implement the real dual-deployment happy path
### Task 9: Implement the five critical negative release scenarios
### Task 10: Implement minimum recoverable-state backup/restore smoke
### Task 11: Build the release-gate CLI, deterministic failure model, and cleanup
### Task 12: Add versioned deployment templates and contract tests
### Task 13: Write modular operator runbooks
### Task 14: Add FAST CI and RELEASE CI adapters
### Task 15: Final integrated release evidence and Phase 1 closure verification

Full task-by-task step detail (files, RED/GREEN sequences, exact interfaces) is authoritative in the human-supplied plan text delivered with this task and is followed exactly during implementation; this repository copy exists to satisfy the "commit the plan" step and as an execution checklist reference. See `docs/devlog/2026-08-31-task15-release-decisions.md` for the T15-D01–D26 decision ledger this plan implements against.
