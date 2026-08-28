# LinuxLab — Claude Code Project Instructions

You are the implementation agent for LinuxLab.

LinuxLab is a long-term open learning and computer-lab platform built around
Django + Wagtail, with Central and offline-first Node deployments.

Your job is to IMPLEMENT the approved architecture, not redesign it.

## Required reading before any implementation

Read these files in this order:

1. `AGENTS.md`
2. `docs/architecture/LinuxLab-Architecture-Spec-v1.md`
3. All ADR files in `docs/adr/`, ADR-001 through ADR-014
4. `docs/plans/Phase1-Learning-Foundation.md`
5. The task-specific prompt explicitly assigned in the current session

If any task conflicts with the Architecture Spec or an ADR:
- STOP implementation of the conflicting part.
- Report the exact conflict.
- Treat the Architecture Spec and ADRs as authoritative.
- Do not silently invent a workaround.

## Architecture invariants

You MUST preserve these rules:

1. One Django/Wagtail codebase.
2. Modular monolith first.
3. Central and Node are deployment profiles, not separate products.
4. Central and Node never replicate application databases.
5. Content, learning state, and execution state remain separate domains.
6. Modules must not directly mutate another module's owned models.
7. Cross-instance identities use stable UUIDs.
8. Attempts always reference immutable published Activity versions.
9. Wagtail editorial revisions are not LinuxLab published versions.
10. Packages are created only from immutable published versions.
11. Node must continue learning while Internet is unavailable.
12. Browser clients must never receive answer keys as hidden data.
13. Lessons must never contain Docker/Podman/runtime-specific implementation details.
14. Human Central passwords must never be copied to Node.
15. Sealed exam submissions must never be silently modified.
16. Do not introduce microservices, Kafka, Kubernetes, React, or another database
    unless a later approved ADR explicitly requires them.

## Current technology baseline

Local development:
- macOS
- PostgreSQL 18 already installed as a system service

Application:
- Python 3.12
- Django 5.2 LTS
- Wagtail 7.4 LTS
- PostgreSQL
- pytest / pytest-django
- Ruff

Use the existing local PostgreSQL installation.
Do not install or downgrade PostgreSQL unless explicitly instructed.

Central and Node development databases must remain separate.

Suggested local databases:

- `linuxlab_central_dev`
- `linuxlab_node_dev`
- `linuxlab_test`

## Python environment isolation — mandatory

LinuxLab MUST use a project-local virtual environment:

`.venv`

Rules:

- `.venv` MUST be created using Python 3.12.
- Never use macOS system Python for LinuxLab development.
- Never install LinuxLab Python dependencies globally.
- Django, Wagtail, psycopg, pytest, pytest-django, Ruff, cryptography,
  and all future Python project dependencies must live inside `.venv`.
- PostgreSQL is NOT part of `.venv`; it remains an external system service.
- Do not install another PostgreSQL version for this project.
- Do not introduce Docker merely to provide local PostgreSQL.
- Commands executed by agents should prefer explicit project-local paths,
  for example:
  `.venv/bin/python`
  `.venv/bin/pip`
  `.venv/bin/pytest`
  `.venv/bin/ruff`
  instead of assuming shell activation persists between commands.
- `pyproject.toml` is the authoritative declaration of Python dependencies.
- `.venv/` must never be committed to Git.

## Development discipline

Use test-driven development.

For every behavior:

1. Write a focused failing test.
2. Run the test and confirm it fails for the expected reason.
3. Implement the minimum code required.
4. Run the focused test.
5. Run the relevant module test suite.
6. Run static checks.
7. Self-review the diff.
8. Commit the completed task.

Do not create code that is not required by the assigned task.

YAGNI is mandatory.

## Task isolation

Work on ONE assigned task only.

Do not begin the next task automatically.

Do not opportunistically implement:
- future phases;
- terminal sandbox;
- exam engine;
- badges;
- classroom management;
- AI;
- Kubernetes;
- unrelated refactors.

unless the current task explicitly requires them.

When the current task is complete:
STOP and report.

## Module boundaries

Each Django app owns its models and data.

Other modules must interact through:
- public service functions;
- explicit contracts;
- domain events.

Do not import another module's model merely to update its data directly.

Reading across module boundaries should also be minimized and must follow the
approved public interface where one exists.

## Database rules

Use PostgreSQL.

Do not rely on SQLite behavior for application correctness.

Database primary keys are local implementation details.

Use stable UUID/public IDs for:
- Central ↔ Node exchange;
- packages;
- events;
- cross-module published references.

Never use a local integer primary key as a distributed identifier.

## Versioning rules

Published definitions are immutable.

Do not update a published ActivityVersion, QuestionVersion,
PublicationSnapshot, ExamVersion, or other immutable published artifact.

Changes create a new version.

Attempts and results retain references to the exact version used.

## Wagtail rules

Use Wagtail for structured authoring and editorial workflow.

Do not store:
- learner progress;
- quiz attempts;
- lab sessions;
- exam submissions

inside Wagtail Page models.

A Lesson may reference an Activity, but must not own its runtime state.

## Security

Never expose:
- answer keys;
- signing private keys;
- Central passwords;
- sandbox host credentials

to the browser or Node packages where they are not required.

Never run arbitrary learner shell commands in the Django web process.

The future Linux execution environment is a separate sandbox boundary.

## Git

Before starting:
- inspect `git status`;
- confirm the intended branch;
- do not modify unrelated files.

Make one meaningful commit per completed task.

Suggested commit formats:

- `chore: ...`
- `feat: ...`
- `test: ...`
- `fix: ...`
- `docs: ...`

Do not push, merge, rebase shared branches, or delete branches without explicit permission.

## Completion report

At the end of every task, report exactly:

### Task completed
- Task:
- Commit:
- Files created:
- Files modified:

### Tests
- Commands run:
- Result:

### Architecture check
- ADRs directly involved:
- Architecture invariants checked:
- Deviations: none / explain

### Concerns
- Any technical debt:
- Any ambiguity:
- Anything the reviewer should inspect:

### Next action
STOP.
Wait for review before starting the next task.