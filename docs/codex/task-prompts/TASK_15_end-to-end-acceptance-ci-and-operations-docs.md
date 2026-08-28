# Codex Task 15 — End-to-end acceptance, CI, and operations docs

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

### Task 15: End-to-end acceptance, CI, and operations docs

**Files:** `tests/e2e/*`, `.github/workflows/test.yml`, `docs/operations/{central,node,package-keys}.md`.

**Produces:** executable proof of the Phase 1 architecture and reproducible operator workflow.

- [ ] E2E: author/publish -> snapshot -> sign `.llpkg` -> import Node -> activate -> disconnect network -> serve learning path.
- [ ] E2E: Node creates local Attempt/Result -> Outbox -> deliver event twice -> Central processes once -> Node records ACK.
- [ ] Run Central and Node against separate PostgreSQL test databases/schemas; never copy application rows directly.
- [ ] CI: Python 3.12 + PostgreSQL 16, install dev dependencies, run `ruff check .`, `pytest`, Central `manage.py check`, Node `manage.py check`.
- [ ] Central operations doc: migrate, create admin, author, publish, signing key generation, package export.
- [ ] Node operations doc: migrate, install trusted public key, import, stage, activate, rollback, LAN bind, offline verification.
- [ ] Key doc: signing private key never ships to Node; key rotation overlaps trusted public keys before old-key retirement.
- [ ] Run the complete verification suite and commit `test: verify LinuxLab Phase 1 learning foundation`.

## Phase 1 acceptance gate

Phase 1 is accepted only when all of these are demonstrated:

1. Wagtail can author Learning Path -> Module -> Lesson structured content.
2. A guest can read published content without an account.
3. Quiz grading is server-side and browser payloads contain no answer key.
4. Attempt stores an exact immutable ActivityVersion.
5. Progress can be rebuilt from Results/events.
6. Publish validates dependencies and freezes a new immutable snapshot.
7. Central builds a signed `.llpkg`; tampering is detected.
8. Node stages, activates, and rolls back packages.
9. Node serves installed learning content with network access blocked.
10. Node event delivery is idempotent under retries.
11. Guest progress is not synchronized until explicitly claimed.
12. The same codebase passes checks under Central and Node profiles.
13. No Architecture Invariant from the approved spec is violated.

## Deferred to later plans

- Browser xterm.js and WebSocket terminal sessions.
- Rootless Podman sandbox execution and validators.
- Formal Exam Engine, autosave/resume, sealed submissions, encrypted exam packages.
- Kiosk/exam-mode desktop controls.
- Python/Git/Database/Network providers.
- Badges/certificates, school/classroom administration, AI tutor.
