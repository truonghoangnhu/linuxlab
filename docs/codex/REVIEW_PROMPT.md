# Codex Review Prompt

Use this after a task commit if you want Codex to review before proceeding.

```text
Review the most recent LinuxLab task commit against:
1. AGENTS.md
2. docs/architecture/LinuxLab-Architecture-Spec-v1.md
3. the matching ADRs
4. the matching task in docs/plans/Phase1-Learning-Foundation.md

Do not modify code yet.
Report findings by severity: Critical, Important, Minor.
For every finding include file/line, violated requirement, why it matters, and the smallest correction.
Also explicitly state whether the task is architecture-compliant and whether it may proceed to the next task.
```
