# Task 15 Development Decision Log — Phase 1 Release Readiness Gate

**Date:** 2026-08-31
**Project:** LinuxLab.vn / LinuxLab Open Lab
**Status:** DESIGN APPROVED — implementation in progress
**Predecessor:** Task 14 second corrective, HEAD `a83b1fb358343394f7e99bd3c9d62838473dea70`, 826 tests passing

This file records the architecture decisions approved for Task 15. These decisions are normative for implementation and review. They must not be silently changed during implementation.

## T15-D01 — Task 15 is the official Phase 1 Release Gate

Task 15 is the final release-readiness task for Phase 1 Learning Foundation. There is no Task 16 whose sole purpose is to close Phase 1.

## T15-D02 — Real dual-deployment E2E over HTTP

Release E2E uses real Central/Node deployments: separate settings profiles, separate PostgreSQL databases, separate Gunicorn processes, real HTTP. Docker is not required.

## T15-D03 — FAST CI and RELEASE CI

CI has two tiers: FAST CI (PR/normal validation) and RELEASE CI (protected-branch integration and exact release SHA).

## T15-D04 — Only the dead sync-backoff Minor enters Task 15

`LINUXLAB_SYNC_BACKOFF_BASE_SECONDS`/`LINUXLAB_SYNC_BACKOFF_CAP_SECONDS` are wired into runtime retry calculation. The other three Task 14 Minors (FileSyncTransport determinism, durable idempotency-conflict audit, claim-path index/schema-version hardening) remain deferred backlog.

## T15-D05 — Repo-native release runner is source of truth

`scripts/phase1_release_gate.py` (with `scripts/release_gate/` helpers) is the release-gate source of truth. CI YAML is a thin adapter that invokes it; it does not reimplement release-gate semantics.

## T15-D06 — Fail-closed ephemeral databases per run

Every gate run uses ephemeral Central/Node databases keyed by a `run_id`, named `linuxlab_gate_central_<run_id>`/`linuxlab_gate_node_<run_id>`. The runner refuses to target known developer or production database names.

## T15-D07 — Release evidence is JSON + Markdown; no automatic tag

Each run writes schema-versioned JSON evidence plus a human-readable Markdown projection. Git tagging/release creation remains a separate, human-controlled action.

## T15-D08 — GitHub Actions is the official CI adapter

No second CI provider is introduced in Task 15.

## T15-D09 — One happy path plus exactly five negative scenarios

Release E2E contains one complete happy path (Central publish → signed `.llpkg` → Node verify/stage/activate → learner workflow → sync → Central Inbox → Node DELIVERED) plus exactly five critical negative scenarios: invalid package signature, invalid/revoked Node credential, ACK loss after durable Inbox, Central unavailable, process restart.

## T15-D10 — Linux production target; macOS development-only

Linux is the supported production target and the release authority for CI. macOS remains a developer workstation environment only.

## T15-D11 — systemd is a reference process manager, not a dependency

LinuxLab application behavior must not depend on systemd.

## T15-D12 — Gunicorn is the Phase 1 production reference server

Used for both Central and Node, and run in RELEASE CI.

## T15-D13 — Caddy is the reference reverse proxy; Central HTTPS

Central production traffic is HTTPS. Node classroom traffic may be HTTP or HTTPS depending on local infrastructure. Gunicorn is never exposed directly to the Internet or classroom LAN in the reference deployment.

## T15-D14 — External config/secrets contract

Production configuration and secrets live outside the repository under `/etc/linuxlab/` (`central.env`, `node.env`, `keys/package-signing.key`), loaded via systemd `EnvironmentFile=`. Repository files never contain production secrets.

## T15-D15 — Minimal `/healthz` and `/readyz` contracts

`/healthz` is liveness only (no DB query, no config disclosure). `/readyz` checks DB connectivity, migration-state compatibility, profile validity, and required profile configuration, and never mutates schema/config or discloses internal failure detail in the HTTP response.

## T15-D16 — Documented and release-tested backup/restore, no backup daemon

Task 15 provides a tested recovery procedure using `pg_dump`/`pg_restore`, not an automated backup service.

## T15-D17 — Public/domain lifecycle seeding only

Release scenarios mutate business state exclusively through existing public/domain lifecycle APIs. Direct ORM/SQL fabrication of trusted state (e.g. `InstalledPackage.objects.create(...)`, `OutboxMessage.objects.create(...)`, `InboxMessage.objects.create(...)`) is forbidden for scenario setup.

## T15-D18 — Ephemeral Ed25519 keypair per release run

Each run generates a fresh signing keypair. Evidence may record algorithm, public-key fingerprint, package SHA-256, and verification result — never the private key. The ephemeral private key is deleted during cleanup.

## T15-D19 — PostgreSQL-only official CI

No SQLite shortcut for any Django-backed test path in official CI.

## T15-D20 — Real HTTP user-surface smoke, no browser automation

Release E2E reaches real learner HTTP routes but does not add Playwright/Selenium/Chromium or any browser automation framework.

## T15-D21 — Sanitized diagnostics on failure with cleanup

CI always cleans up ephemeral resources while preserving sanitized evidence/logs. `--keep-on-failure` is a local-development-only debugging feature and is rejected when `CI=true`.

## T15-D22 — Explicit migration step; no startup auto-migrate

Migrations are an explicit operator/release step, never run from Gunicorn/Django application startup or from `ExecStartPre=` in the reference systemd units.

## T15-D23 — Minimum recoverable business-state restore verification

Backup/restore smoke verifies actual application-level state after restore (published content, package metadata, NodeRegistration, durable Inbox on Central; active package, learner/runtime state, pending Outbox with successful post-restore sync on Node) — not just `pg_restore` command success.

## T15-D24 — Modular operator runbooks

Operator documentation is a modular set under `docs/operator/` (README, installation, central, node, configuration, deployment, backup-restore, sync-operations, troubleshooting, release checklist).

## T15-D25 — Versioned deployment templates

systemd unit files, Caddy example configs, and env examples are version-controlled reference artifacts under `deploy/`, with no real secrets and no automatic installer/provisioner.

## T15-D26 — RELEASE CI runs Gunicorn and is required on protected-branch/exact-SHA

RELEASE CI is mandatory on merges to the protected integration/release branch and again on the exact commit SHA selected for a release/tag. A PASS from a different SHA is not release evidence for the selected commit.

---

No implementation-time decision may silently contradict these approved choices. A material new architecture decision requires human approval and a new Task 15 decision entry (T15-D27+); none has been added during implementation.
