# Codex Task 12 — Node package verify -> stage -> activate -> rollback

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

### Task 12: Node package verify -> stage -> activate -> rollback

**Files:** `linuxlab/sync/models.py`, `services.py`, tests and migration.

**Produces:** `InstalledPackage`; `import_package()`, `activate_package()`, `rollback_package()`.

- [ ] Test tampered packages are rejected before database payload insertion.
- [ ] Import verified package as `staged` only on a Node-capable profile.
- [ ] Activate transactionally: archive the currently active package of the same type, then activate selected package.
- [ ] Rollback only to a previously verified local immutable package.
- [ ] Test staged update does not change content served until explicit activation.
- [ ] Migrate; run lifecycle/rollback tests; commit `feat: add Node package activation and rollback`.
