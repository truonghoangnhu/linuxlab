# LinuxLab — Instructions for Codex and Other Coding Agents

## Authority order

When instructions conflict, use this order:

1. `docs/architecture/LinuxLab-Architecture-Spec-v1.md`
2. Accepted ADR files in `docs/adr/`
3. `docs/plans/Phase1-Learning-Foundation.md`
4. The current task prompt
5. Existing implementation conventions, once code exists

Never silently weaken an architecture invariant to make a task easier.

## Core architectural rules

- Maintain **one Django/Wagtail codebase** with Central and Node deployment profiles.
- Do not create Central and Node forks.
- Use PostgreSQL for both Central and Node.
- Never replicate application databases between Central and Node.
- A Django app owns its own models. Another app may change that state only through a public service contract or domain event.
- Use UUIDs for cross-instance identities. Local database primary keys are private implementation details.
- Attempts must reference exact immutable Activity versions.
- Wagtail revisions are editorial state; LinuxLab PublishedVersions are learning/sync state.
- Build packages only from immutable published artifacts.
- Guest learning requires no account; anonymous progress is local by default.
- Never send answer keys to learner browsers.
- Lesson/content definitions must not contain Docker/Podman runtime details.
- Node functionality must not require continuous Internet.
- Do not add React/Vue, Kafka/RabbitMQ, Kubernetes, microservices, terminal sandboxes, or formal Exam Engine code during Phase 1.

## Development method

For every task:

1. Read the entire task prompt before modifying files.
2. Inspect the repository and existing tests first.
3. Write the failing test before the implementation whenever the task is testable.
4. Run the narrow test and confirm the expected failure.
5. Implement the smallest change that makes the test pass.
6. Run the narrow test again.
7. Run related app tests.
8. Run `ruff check .` for changed Python code.
9. Run `python manage.py check` for relevant Central/Node settings when configuration is touched.
10. Self-review the diff against the Architecture Spec and current task.
11. Commit only the current task with the commit message specified in the plan.
12. Stop and report. Do not begin the next task unless explicitly instructed.

## Reporting contract after each task

Return:

- Task number and title
- Status: DONE / DONE_WITH_CONCERNS / BLOCKED
- Files created/modified
- Migrations created
- Tests run and exact result
- Django checks run and exact result
- Ruff result
- Commit hash and commit subject
- Any architecture concern or assumption

## Scope discipline

Do not opportunistically refactor unrelated code. Do not add speculative abstractions. If the current task exposes a real architecture conflict, stop implementation and explain the conflict with references to the spec/ADR rather than improvising a new architecture.

## Security baseline

- Secrets never enter Git.
- `.env*` files with secrets must be ignored.
- Package signing private keys never ship to Node.
- Learner payloads use explicit allow-lists.
- Server-side grading owns answer keys.
- Any later command execution must happen behind a dedicated sandbox boundary, never through arbitrary shell execution in the Django web process.
