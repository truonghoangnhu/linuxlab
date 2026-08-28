# Codex Task 14 — Idempotent Outbox/Inbox event sync

## Mandatory pre-read

Before making changes, read:

1. `AGENTS.md`
2. `docs/architecture/LinuxLab-Architecture-Spec-v1.md`
3. relevant accepted ADR files in `docs/adr/`
4. this task in full

## Execution rules

- Execute this task only.
- Use TDD. Observe the focused test fail before implementing the behavior.
- Preserve all architecture invariants.
- Do not add future-phase features.
- Use PostgreSQL 18 locally.
- Run focused tests, related tests, Ruff, and relevant Django checks.
- Commit using the task commit subject stated below.
- Stop after the commit and return the report required by `AGENTS.md`.

## Task requirements

### Task 14: Idempotent Outbox/Inbox event sync

**Files:** `linuxlab/sync/{models,contracts,services,transports,api}.py`, tests and migration.

**Produces:** `OutboxMessage`, `InboxMessage`, `SyncTransport`, HTTP/File transports, `flush_outbox()`, `/api/v1/sync/events/`.

- [ ] Test delivering the same event UUID twice produces one Central processing effect and ACKs both deliveries.
- [ ] Persist OutboxMessage in the same transaction as authoritative local Result/domain event.
- [ ] Implement Central Inbox uniqueness on `event_id` and `get_or_create` processing.
- [ ] Implement retry: an unacknowledged Outbox row remains pending; an ACK marks `acknowledged_at`.
- [ ] Implement `HttpSyncTransport`; implement a deterministic `FileSyncTransport` using exactly the same event envelope for offline/USB-style integration tests.
- [ ] Migrate; run duplicate/retry tests; commit `feat: add idempotent Central Node event sync`.
