# ADR-001 — Django + Wagtail as the Platform Foundation

**Status:** Accepted

## Context

LinuxLab needs a long-lived Python web platform that can combine structured educational content, custom learning logic, offline Node deployments, future assessment features, and later interactive Linux practice. The platform must remain maintainable by a small team and must not be constrained by a traditional LMS product model.

## Decision

Use Django as the application framework and Wagtail as the structured authoring/editorial layer.

Django owns application logic, authentication primitives, APIs, learning state, synchronization, assessment, and future execution services. Wagtail owns structured content authoring, editorial revisions, preview, review, and publication workflows.

Wagtail is part of the Django project, not a separate service and not the owner of all LinuxLab business logic.

## Consequences

- LinuxLab remains a normal Django codebase that can grow beyond CMS features.
- Authors gain a mature structured editing workflow without LinuxLab having to build its own CMS.
- Learning state and execution state must not be embedded into Wagtail page models merely for convenience.
- A later headless/mobile frontend remains possible through explicit APIs without replacing the content source of truth.
