# LinuxLab Development Kit — Start Here

This repository packet is the development baseline for LinuxLab.

## Recommended workflow

1. Create an empty local Git repository on macOS and open it in VS Code.
2. Copy this packet into the repository root.
3. Read `AGENTS.md` first. Codex should also read it before making changes.
4. Read `docs/architecture/LinuxLab-Architecture-Spec-v1.md`.
5. Read `docs/adr/ADR-001-django-wagtail-foundation.md` through `docs/adr/ADR-014-authoring-publishing.md`.
6. Read `docs/plans/Phase1-Learning-Foundation.md`.
7. Prepare Python 3.12 and PostgreSQL 18 using `docs/development/MACOS_VSCODE_SETUP.md`.
8. Ask Codex to execute **one task at a time**, starting with Task 1, using the prompt files in `docs/codex/task-prompts/`.
9. Review each task before allowing Codex to continue.

## Development order

```text
Phase 1      Learning Foundation
   ↓
Phase 1.5    Interactive Linux Lab
   ↓
Phase 2      Offline Exam Engine
   ↓
Phase 3      Advanced Activities / LMS expansion
```

Do not build Phase 1.5 features during Phase 1. The Activity contract exists specifically so later capabilities can be added without changing the Phase 1 core.

## Local environment target

- macOS
- VS Code
- Python 3.12
- Django 5.2 LTS
- Wagtail 7.4 LTS
- PostgreSQL 18 locally
- Git
- Codex for implementation

PostgreSQL 18 is suitable for local development because Django 5.2 supports PostgreSQL 14 and higher. The architecture baseline remains PostgreSQL 16+ so production deployment is not unnecessarily tied to one major version.
