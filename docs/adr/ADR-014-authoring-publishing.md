# ADR-014 — Structured Authoring & Immutable Publishing

**Status:** Accepted

## Context

Wagtail is excellent for drafting and editorial revisions, but Node/offline learning and formal assessment need stable, reproducible artifacts rather than mutable live CMS records.

## Decision

Use Wagtail for structured authoring and editorial revisions. Publication performs validation, dependency resolution, exact-version freezing, and creation of an immutable LinuxLab PublishedVersion. Packages are built only from published snapshots.

Node receives verified signed packages, stages them, activates them explicitly, and can roll back to an earlier verified package.

## Consequences

- Editing a draft cannot change an active Node lesson or historical Attempt.
- Package publication becomes a quality gate rather than a simple visibility switch.
- Author/reviewer/publisher can be one person initially and split into roles later without changing the data model.
