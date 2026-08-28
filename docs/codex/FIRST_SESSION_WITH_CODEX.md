# First Session With Codex

After copying this kit into the empty repository and preparing PostgreSQL 18:

1. Create and activate `.venv`.
2. Commit the documentation baseline.
3. Create the Phase 1 branch:

```bash
git switch -c feature/phase1-learning-foundation
```

4. Open Codex in the repository.
5. Paste `CODEX_MASTER_PROMPT.md`.
6. Then send:

```text
Execute only docs/codex/task-prompts/TASK_01_bootstrap-django-wagtail-and-central-node-profiles.md.
Use the local PostgreSQL 18 installation. Do not start Task 2.
```

7. When Codex returns, inspect:

```bash
git status --short
git log -3 --oneline
git show --stat --oneline HEAD
```

8. Ask Codex for a review using `REVIEW_PROMPT.md` if desired.
9. Only after Task 1 is clean, send the Task 2 prompt.

This task-by-task workflow is intentionally slower than "build everything" in one prompt, but much faster overall because failures stay localized and architecture drift is caught immediately.
