# Codex Task Execution Protocol

For every task, give Codex one task prompt file only after the previous task is reviewed.

## Controller message

```text
Read AGENTS.md and the Architecture Spec first.
Execute only: docs/codex/task-prompts/TASK_NN_*.md
Do not begin the next task.
Use TDD and commit the completed task.
Return the required task report.
```

## Review before continuing

Check:

- `git status --short` is clean;
- task commit exists;
- tests claimed by Codex actually ran;
- no out-of-scope dependencies/features were introduced;
- diff respects module ownership and immutable-version rules;
- no answer keys or secrets appear in client code;
- no future-phase work was mixed into Phase 1.

If the task is clean, give the next prompt. If not, ask Codex to fix the current task only and re-run its tests.
