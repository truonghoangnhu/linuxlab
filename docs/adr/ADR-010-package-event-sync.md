# ADR-010 — Package/Event Synchronization

**Status:** Accepted

## Context

Central and Node need reliable synchronization while permitting long offline periods. Database replication would couple schemas, increase conflict complexity, and undermine standalone Nodes.

## Decision

Content/configuration flows downward as immutable, versioned, signed packages. Learning activity/results flow upward as idempotent events. Node writes authoritative local activity state first and synchronizes later through an Outbox/Inbox pattern.

Transport is independent from the package/event format so HTTPS, LAN, file, or later USB workflows can use the same contracts.

## Consequences

- Retry is safe through globally unique event IDs.
- Node operation is independent of sync availability.
- Content ownership remains Central; local activity ownership remains the Node where created until synchronized.
