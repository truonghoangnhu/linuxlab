# Codex Task 09 — Publication validation and immutable snapshots

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

### Task 9: Publication validation and immutable snapshots

**Files:** `linuxlab/publication/{models,validators,snapshots}.py`, tests and migration.

**Produces:** `PublicationSnapshot`; `validate_learning_path()`; `publish_learning_path()`.

- [ ] Test publication fails when a referenced ActivityVersion or QuestionVersion is unpublished/missing.
- [ ] Implement deterministic canonical JSON and SHA-256 hashing.
- [ ] Implement snapshot model `{kind,source_public_id,version,payload,content_hash,created_at}` with unique version constraint.
- [ ] Resolve exact dependency versions; never serialize `latest`.
- [ ] Test changing a Wagtail draft and republishing creates N+1 without mutating N.
- [ ] Migrate; run publication tests; commit `feat: add immutable publication snapshots`.
