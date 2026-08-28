# Codex Task 05 — Activity contract, registry, and immutable versions

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

### Task 5: Activity contract, registry, and immutable versions

**Files:** `linuxlab/activities/contracts.py`, `registry.py`, `models.py`, `services.py`, tests and migration.

**Produces:** `ActivityProvider` protocol; `ActivityRegistry`; `ActivityDefinition`; `ActivityVersion`; `get_activity_version()`.

- [ ] Test provider registration, duplicate provider rejection, and unknown provider lookup.
- [ ] Define provider methods `validate_config(config)` and `evaluate(config, submission)` for Phase 1; later lifecycle methods extend this without exposing provider internals to Learning.
- [ ] Implement `ActivityDefinition(name,type_key,public_id)`.
- [ ] Implement `ActivityVersion(definition,version,config,required_capabilities,published_at)` with unique `(definition,version)`.
- [ ] Implement publication service so an already-published row is never edited; a change creates version N+1.
- [ ] Test exact-version lookup by definition UUID + version.
- [ ] Migrate; run Activity tests; commit `feat: add versioned activity provider contract`.
