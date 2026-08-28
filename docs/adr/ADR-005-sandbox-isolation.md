# ADR-005 — Dedicated Sandbox Boundary for Command Execution

**Status:** Accepted

## Context

LinuxLab intends to support real Linux command practice in the browser. Executing arbitrary learner commands inside the Django web process or directly on the application host would create unacceptable security and reliability risks.

## Decision

All learner-controlled command execution must occur behind a dedicated sandbox boundary.

The Django/Wagtail application may request, track, and evaluate sandbox sessions, but it must never execute arbitrary learner shell commands in the web process. The runtime is accessed through a Sandbox Provider abstraction. The initial server-side implementation is planned as rootless Podman with constrained CPU, memory, process count, filesystem, network, privileges, and TTL.

Browser terminal UI technology is separate from the sandbox itself; xterm.js is only a terminal presentation layer.

## Consequences

- A compromised or intentionally destructive learner command is isolated from the Django process and host as far as the selected sandbox technology permits.
- Lesson definitions reference LabProfile/Sandbox capabilities rather than Docker/Podman commands.
- Sandbox implementation can later change without rewriting content or learning models.
- Phase 1 does not implement this runtime; Phase 1.5 introduces it behind the boundary established here.
