# Codex Task 13 — Offline Node serving

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

### Task 13: Offline Node serving

**Files:** `linuxlab/sync/views.py`, `urls.py`, templates, tests.

**Produces:** LAN learning route backed only by active local package data.

- [ ] Test a Node serves an active LearningPath while all socket/network connection attempts are patched to fail.
- [ ] Implement local active-package query service against Node PostgreSQL only.
- [ ] Add `/learn/<uuid>/` Node route rendering package payload.
- [ ] Reject this snapshot-serving route under the Central profile.
- [ ] Run Node test with network blocked; commit `feat: serve active learning packages offline on Node`.
