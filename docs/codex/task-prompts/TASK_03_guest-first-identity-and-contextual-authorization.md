# Codex Task 03 — Guest-first identity and contextual authorization

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

### Task 3: Guest-first identity and contextual authorization

**Files:** `linuxlab/identity/models.py`, `services.py`, `policies.py`, tests and migration.

**Produces:** `Account`, `Learner`, `AnonymousLearner`, `LearningContext`, `RoleAssignment`, `ensure_learner()`, `can()`.

- [ ] Test that `Account` and `Learner` have different UUIDs and that `AnonymousLearner` has no Account dependency.
- [ ] Implement a custom `Account(AbstractUser, StableIdentityModel)` from day one.
- [ ] Implement `Learner(account nullable one-to-one)`, `AnonymousLearner`, `LearningContext(kind,name)`, and contextual `RoleAssignment`.
- [ ] Implement `ensure_learner(account)`.
- [ ] Implement `can(actor, action, resource=None, context=None)` using role/action mappings and context scope; do not add `is_teacher`/`is_student` fields.
- [ ] Test that an Instructor role in context A grants access in A and denies the same action in context B.
- [ ] Migrate; run identity tests; commit `feat: add guest-first identity and contextual roles`.
