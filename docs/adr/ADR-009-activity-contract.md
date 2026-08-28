# ADR-009 — Activity Contract Architecture

**Status:** Accepted

## Context

LinuxLab must grow from quizzes into Terminal, Python, Git, Database, Network, and future activity types without repeatedly modifying the Learning core.

## Decision

Represent interactive learning as versioned Activities. Learning communicates through a stable Activity provider contract. Concrete activity implementations live in independent modules/providers.

Phase 1 requires provider configuration validation and evaluation. Later lifecycle capabilities may extend the contract without exposing implementation details to Learning.

## Consequences

- New activity types can be added without rewriting Lesson or Learning core.
- Attempts reference exact Activity versions.
- Provider implementation details remain outside content models.
