# ADR-002 — Modular Monolith First

**Status:** Accepted

## Context

LinuxLab must support several capability domains—content, learning, activities, assessment, synchronization, Node operation, catalog, later labs, and exams—but the initial team and deployment scale do not justify distributed infrastructure.

Premature microservices would increase operational cost, deployment complexity, testing surface, and failure modes before there is measured need.

## Decision

Implement LinuxLab initially as one modular Django monolith with clearly bounded Django apps.

Each module owns its models and exposes explicit public service interfaces and/or domain events. Module boundaries are designed so a component can be extracted later if scale, security isolation, or independent deployment creates a measured reason.

Do not introduce microservices, Kafka/RabbitMQ, Kubernetes, or independent databases merely to imitate large-platform architecture.

## Consequences

- Development and deployment remain simple enough for a small team.
- Module boundaries must be enforced in code review rather than assumed from directory names.
- Later extraction is possible, but only after real operational evidence justifies the cost.
- This ADR is refined by ADR-011 for module data ownership and public service boundaries.
