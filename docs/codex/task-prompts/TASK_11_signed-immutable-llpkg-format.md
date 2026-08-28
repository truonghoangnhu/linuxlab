# Codex Task 11 — Signed immutable `.llpkg` format

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

### Task 11: Signed immutable `.llpkg` format

**Files:** `linuxlab/publication/package.py`, `signing.py`, tests.

**Produces:** `PackageManifest`; `build_package()`; `verify_package()`.

- [ ] Define v1 archive members exactly: `manifest.json`, `payload.json`, `signature.bin`.
- [ ] Manifest fields: schema version, package UUID, package type, package version, created timestamp, payload SHA-256, minimum LinuxLab version, `ed25519` signature algorithm.
- [ ] Build from a PublicationSnapshot only.
- [ ] Sign canonical manifest+payload bytes with Ed25519; Node only needs the public key.
- [ ] Test valid package verifies and a one-byte modification is rejected.
- [ ] Commit `feat: add signed immutable LinuxLab packages`.
