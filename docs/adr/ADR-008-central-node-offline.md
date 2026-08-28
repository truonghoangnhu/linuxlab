# ADR-008 — Central–Node Offline Architecture

**Status:** Accepted

## Context

LinuxLab must serve Internet-facing self-learning while also operating in school computer labs where connectivity may be weak or absent. A Node must continue learning, later lab practice, and later exams without a live Central connection.

## Decision

Use one Django/Wagtail codebase with Central and Node deployment profiles. Central is authoritative for authoring/publication. Node stores verified published packages and local learning state. Central and Node do not replicate application databases.

Node is offline-first; Central is online-first.

## Consequences

- The same code is maintained for both profiles.
- Node can remain usable during Internet failure.
- Synchronization requires explicit package/event contracts.
- Features must declare whether they are Central-only, Node-capable, or both.
