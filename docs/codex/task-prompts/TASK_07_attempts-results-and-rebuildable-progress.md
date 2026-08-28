# Codex Task 07 — Attempts, Results, and rebuildable Progress

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

### Task 7: Attempts, Results, and rebuildable Progress

**Files:** `linuxlab/learning/{models,services,projections}.py`, tests and migration.

**Produces:** `start_attempt()`, `submit_attempt()`, `rebuild_progress()`.

- [ ] Test that Attempt creation rejects unpublished ActivityVersion and stores the exact published version FK.
- [ ] Implement `Attempt(learner,activity_version,started_at,submitted_at,submission)` and immutable `Result(attempt,score,max_score,details)`.
- [ ] Implement `Progress(learner,activity_definition,completed,best_score)` as derived state with a unique learner/activity constraint.
- [ ] Implement `submit_attempt()` through ActivityRegistry, persist Result, and emit `activity.completed` in the same transaction.
- [ ] Implement `rebuild_progress()` from authoritative Results; completion threshold is read from Activity config rather than hard-coded globally.
- [ ] Test deleting Progress then rebuilding yields the same completion/best score.
- [ ] Migrate; run learning tests; commit `feat: add attempts results and rebuildable progress`.
