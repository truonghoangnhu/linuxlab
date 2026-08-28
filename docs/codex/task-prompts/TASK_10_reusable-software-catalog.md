# Codex Task 10 — Reusable software catalog

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

### Task 10: Reusable software catalog

**Files:** `linuxlab/catalog/{models,services}.py`, tests and migration.

**Produces:** `Software`, `Distribution`, `InstallationRecipe`, `get_install_command()`.

- [ ] Test one LibreOffice catalog record can return distro-specific install commands.
- [ ] Implement stable UUID models and unique software/distribution recipe relationship.
- [ ] Add service lookup by UUIDs; Lessons reference catalog entities rather than duplicating metadata.
- [ ] Migrate; run catalog tests; commit `feat: add reusable software catalog`.
