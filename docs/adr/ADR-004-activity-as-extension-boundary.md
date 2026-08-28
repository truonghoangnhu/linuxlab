# ADR-004 — Activity as the Learning Extension Boundary

**Status:** Accepted

## Context

LinuxLab begins with reading and lightweight quizzes but is intended to grow into Terminal, Python, Git, Database, Network, and future activity types. Hard-coding each new interactive capability into Lesson or Learning would make the core progressively more fragile.

## Decision

Model interactive learning through a generic, versioned Activity abstraction. Lessons reference exact Activity versions instead of embedding provider implementation details.

Activity is the stable extension boundary between authored content and concrete interactive behavior.

This ADR establishes the high-level boundary. ADR-009 refines it into the formal Activity Provider contract, registry, evaluation lifecycle, and immutable version rules.

## Consequences

- New interactive capabilities can be introduced as modules/providers.
- Lesson and Learning core remain small.
- Attempts must reference exact immutable Activity versions.
- Provider-specific configuration must stay outside lesson presentation fields except through stable references.
