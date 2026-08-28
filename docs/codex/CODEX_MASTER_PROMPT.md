# Master Prompt for Codex

Use the following as the first instruction in a fresh Codex development session.

---

You are implementing LinuxLab, a long-lived Django/Wagtail platform for self-learning, offline school computer labs, and later interactive Linux practice and computer-based exams.

Before changing code, read these files in order:

1. `AGENTS.md`
2. `docs/architecture/LinuxLab-Architecture-Spec-v1.md`
3. `docs/adr/ADR-001-django-wagtail-foundation.md` through `ADR-014-authoring-publishing.md`
4. `docs/plans/Phase1-Learning-Foundation.md`
5. the task prompt I name in `docs/codex/task-prompts/`

The Architecture Spec is binding. Do not weaken architecture invariants to simplify implementation.

Work on exactly **one Phase 1 task at a time**. Use TDD: create/observe a failing test, implement the smallest correct change, run the focused tests, run related tests, run Ruff, and run relevant Django checks. Make the task-level commit specified by the plan. Then stop and report; do not automatically start the next task.

This Mac uses PostgreSQL 18 locally. Treat Central and Node as separate databases. Do not add terminal sandbox, formal exam functionality, React/Vue, Kafka/RabbitMQ, Kubernetes, or microservices during Phase 1.

If the plan conflicts with the Architecture Spec, stop and report the conflict before coding. If existing code conflicts with the Architecture Spec, prefer the Spec and explain the migration/fix needed.

At task completion report: status, changed files, migrations, tests and exact results, Django checks, Ruff result, commit hash, and concerns.

Begin by inspecting the repository and executing only the task prompt I provide.

---
