# Codex Task 08 — Anonymous local progress and explicit claim flow

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

### Task 8: Anonymous local progress and explicit claim flow

**Files:** `linuxlab/learning/static/learning/guest_progress.js`, `views.py`, URLs, tests.

**Produces:** localStorage key `linuxlab.guest.progress.v1`; authenticated `POST /api/v1/learning/claim-progress/`.

- [ ] Implement browser-local anonymous completion storage; no background sync for guests.
- [ ] Test claim endpoint requires authentication.
- [ ] Validate every claimed UUID/version against a published ActivityVersion.
- [ ] Resolve Account -> Learner with `ensure_learner()` and create/rebuild durable progress only for explicitly claimed items.
- [ ] Test unrelated anonymous browser data is not uploaded.
- [ ] Run claim tests; commit `feat: add local guest progress claim flow`.
