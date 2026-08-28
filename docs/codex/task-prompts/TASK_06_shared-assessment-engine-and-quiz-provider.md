# Codex Task 06 — Shared Assessment engine and Quiz provider

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

### Task 6: Shared Assessment engine and Quiz provider

**Files:** `linuxlab/assessment/{models,providers,services}.py`, `linuxlab/quiz/provider.py`, tests and migration.

**Produces:** `Question`, immutable `QuestionVersion`, single-choice grading, learner-safe serialization, `AssessmentActivityProvider`.

- [ ] Test server-side grading for correct/incorrect single-choice answers.
- [ ] Test that learner serialization contains prompt/options but cannot contain `answer_key` or a `correct` marker.
- [ ] Implement `Question` and versioned `QuestionVersion(type_key,prompt,options,answer_key,scoring,published)`.
- [ ] Implement `SingleChoiceProvider.grade()`.
- [ ] Implement `serialize_for_learner()` as an allow-list, not a model dump.
- [ ] Implement Assessment Activity config as an ordered list of exact `{question_public_id, version}` references.
- [ ] Migrate; run assessment/quiz tests; commit `feat: add shared assessment and quiz provider`.
