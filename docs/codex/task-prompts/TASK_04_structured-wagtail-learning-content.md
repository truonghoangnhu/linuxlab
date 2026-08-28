# Codex Task 04 — Structured Wagtail learning content

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

### Task 4: Structured Wagtail learning content

**Files:** `linuxlab/content/blocks.py`, `models.py`, templates, tests and migration.

**Produces:** `LearningPathPage -> ModulePage -> LessonPage`; `CommandBlock`, `TipBlock`, `ActivityReferenceBlock`.

- [ ] Test the allowed Wagtail page hierarchy.
- [ ] Add stable UUID `public_id` to the custom page base.
- [ ] Implement Lesson `StreamField` blocks for heading, rich text, command, tip, and Activity reference `{activity_public_id, activity_version}`.
- [ ] Render Lesson content without reading/writing Progress on page models.
- [ ] Test round-trip serialization of an Activity reference block.
- [ ] Migrate; run content tests; commit `feat: add structured Wagtail learning content`.
