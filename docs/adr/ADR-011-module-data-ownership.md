# ADR-011 — Module Data Ownership & Service Boundaries

**Status:** Accepted

## Context

A long-lived Django monolith becomes difficult to maintain when apps directly update one another's models.

## Decision

Each LinuxLab module owns its models and persistent state. Other modules change that state only through public service contracts or domain events. Cross-instance identity uses UUID + immutable version; database primary keys stay local implementation details.

Wagtail revisions represent editorial state. LinuxLab PublishedVersions represent learning, package, sync, and exam state.

## Consequences

- Module boundaries are enforceable inside the monolith.
- Future service extraction remains possible without requiring it now.
- Public service names and domain events become important compatibility contracts.
