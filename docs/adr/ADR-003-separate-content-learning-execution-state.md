# ADR-003 — Separate Content, Learning State, and Execution State

**Status:** Accepted

## Context

A LinuxLab lesson contains authored educational material, while a learner produces attempts/progress and an interactive lab produces runtime sessions/results. Combining those concerns in Wagtail pages would make publication, historical reproducibility, offline packaging, and later sandbox replacement difficult.

## Decision

Treat the following as separate architectural state domains:

1. **Content state** — authored/reviewed/published learning material managed through Wagtail and publication snapshots.
2. **Learning state** — learner attempts, results, progress, completion, and later achievements.
3. **Execution state** — short-lived terminal/sandbox/lab sessions and validator results.

Content may reference a versioned Activity, but it must not own learner progress or container/runtime details. Learning may record the result of an Activity, but it must not implement sandbox behavior. Execution may evaluate work, but it must not mutate authored lesson content.

## Consequences

- Wagtail revisions can evolve without rewriting learner history.
- Execution technology can change without editing lesson content.
- Published learning packages can remain deterministic and reproducible.
- Cross-domain interaction requires explicit identifiers, versions, and service contracts.
