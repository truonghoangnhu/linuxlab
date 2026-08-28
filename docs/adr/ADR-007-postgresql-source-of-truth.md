# ADR-007 — PostgreSQL as the Source-of-Truth Database per Deployment

**Status:** Accepted

## Context

LinuxLab Central and school Nodes will store structured content metadata, attempts, progress, package state, synchronization outboxes/inboxes, and later exam autosave data. Nodes may serve many simultaneous browsers during a class or exam.

Using different database engines for Central and Node would introduce behavior differences and migration/test complexity. SQLite is attractive for demos but is not the desired long-lived Node source of truth.

## Decision

Use PostgreSQL as the primary source-of-truth database for every LinuxLab deployment instance, including Central and Node.

Central and each Node own independent PostgreSQL databases. Database primary keys are local implementation details; UUIDs and immutable versions are used for cross-instance identity.

PostgreSQL database replication is not the LinuxLab Central–Node synchronization mechanism. ADR-008 and ADR-010 define offline Node architecture and package/event synchronization.

## Consequences

- Central and Node exercise the same database semantics.
- Local development should use separate Central and Node databases to expose accidental coupling early.
- Node deployment requires PostgreSQL administration, but gains stronger concurrency and durability for classroom/exam workloads.
- Application-level sync remains explicit and auditable rather than hidden in database replication.
