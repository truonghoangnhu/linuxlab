# Codex Task 02 — Stable identities and domain event primitive

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

### Task 2: Stable identities and domain event primitive

**Files:** `linuxlab/core/models.py`, `linuxlab/core/events.py`, tests and migration.

**Produces:** `StableIdentityModel.public_id`; `DomainEvent`; `publish_event(event_type, payload, aggregate_id)`.

- [ ] Test that two events receive distinct UUID event IDs and preserve JSON payload.
- [ ] Implement abstract `StableIdentityModel` with unique UUID `public_id`.
- [ ] Implement `DomainEvent(event_id, event_type, aggregate_id, payload, created_at, processed_at)`.
- [ ] Implement `publish_event()` as the only creation helper for general domain events.
- [ ] Migrate; run `pytest linuxlab/core/tests -v`.
- [ ] Commit `feat: add stable identities and domain events`.
