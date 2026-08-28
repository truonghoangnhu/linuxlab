# Git Workflow

## Branching

Keep `main` stable. Develop Phase 1 on a feature branch:

```bash
git switch -c feature/phase1-learning-foundation
```

For stronger isolation, Codex/Superpowers may create a Git worktree, but do not create one inside another worktree.

## Commit discipline

One reviewed task = one logical commit whenever practical.

Use the commit subject from the Phase 1 plan. Examples:

```text
chore: bootstrap LinuxLab Django Wagtail profiles
feat: add stable identities and domain events
feat: add guest-first identity and contextual roles
```

Do not squash task history during active development; the task commits are useful review checkpoints.

## Before each task

```bash
git status --short
git log -5 --oneline
```

The tree should be clean before starting the next task.

## Before merging Phase 1

Run the complete acceptance gate, inspect migrations, inspect `git diff main...HEAD`, and perform an architecture review against `LinuxLab-Architecture-Spec-v1.md`.
