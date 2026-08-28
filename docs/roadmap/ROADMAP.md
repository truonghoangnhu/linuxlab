# LinuxLab Development Roadmap

## Phase 1 — Learning Foundation

Deliver the architecture proof:

- Django/Wagtail modular monolith
- Central and Node profiles
- guest-first identity
- structured learning content
- versioned Activities
- lightweight Assessment/Quiz
- Attempts, Results, rebuildable Progress
- immutable publication snapshots
- software catalog
- signed `.llpkg`
- Node stage/activate/rollback
- offline Node serving
- idempotent event sync

## Phase 1.5 — Interactive Linux Lab

Add the distinctive LinuxLab practice layer without changing Phase 1 learning/content contracts:

- xterm.js UI
- WebSocket session transport
- sandbox service boundary
- rootless Podman provider
- LabProfile versions
- validators
- TTL and resource limits
- Linux Basics terminal exercises

## Phase 2 — Exam

- Question Bank expansion
- Exam/ExamVersion/ExamSession
- Candidate and identity assurance
- deterministic variants
- autosave/resume
- sealed submissions
- audit log
- signed/encrypted exam packages
- offline Node exam
- result sync

## Phase 3 — Advanced activities and LMS expansion

- Python, Git, Database, and Network Activity providers
- multi-container labs only where genuinely required
- analytics
- optional badge/certificate module
- optional classroom/school module
- optional browser/WASM Quick Try mode

The roadmap is capability-driven. Do not implement a later-phase feature merely because it is easy to add while touching an earlier task.
