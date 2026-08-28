# Codex Task 01 — Bootstrap Django/Wagtail and Central/Node profiles

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

### Task 1: Bootstrap Django/Wagtail and Central/Node profiles

**Files:** `pyproject.toml`, `manage.py`, `pytest.ini`, `.env.example`, `linuxlab/settings/*.py`, `linuxlab/urls.py`, `linuxlab/core/tests/test_settings_profiles.py`

**Produces:** `LINUXLAB_PROFILE`; `central`, `node`, and `test` settings modules.

- [ ] Write failing tests asserting `settings.LINUXLAB_PROFILE == "test"` and PostgreSQL engine is configured.
- [ ] Add dependencies with exact floors/ranges: Python `>=3.12,<3.13`, Django `>=5.2,<5.3`, Wagtail `>=7.4,<7.5`, psycopg `>=3.2,<4`, pytest `>=8,<9`, pytest-django `>=4.9,<5`, Ruff `>=0.12,<1`, cryptography `>=45,<46`.
- [ ] Create `base.py`; set `AUTH_USER_MODEL = "identity.Account"`; configure Wagtail and PostgreSQL from environment variables.
- [ ] Create `central.py` with `LINUXLAB_PROFILE="central"`, `LINUXLAB_CAN_AUTHOR=True`, `LINUXLAB_CAN_IMPORT_PACKAGES=False`.
- [ ] Create `node.py` with `LINUXLAB_PROFILE="node"`, `LINUXLAB_CAN_AUTHOR=False`, `LINUXLAB_CAN_IMPORT_PACKAGES=True`.
- [ ] Run `pytest linuxlab/core/tests/test_settings_profiles.py -v` and `manage.py check` under both profiles.
- [ ] Commit `chore: bootstrap LinuxLab Django Wagtail profiles`.
