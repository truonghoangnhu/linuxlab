# LinuxLab operator documentation

LinuxLab is one Django/Wagtail codebase deployed as two profiles, Central
and Node — never separate forks, never a shared or replicated database.
This directory is the operator-facing counterpart to the architecture and
implementation docs: it documents what is actually shipped and runnable
today, not aspirational future features.

Start here based on what you're trying to do:

## I'm setting up a new host

1. [installation.md](installation.md) — production prerequisites (Linux,
   Python 3.12, PostgreSQL 18, Gunicorn, Caddy, systemd) and the initial
   application install.
2. [configuration.md](configuration.md) — every environment
   variable/setting, classified COMMON / CENTRAL-ONLY / NODE-ONLY /
   SECRET / OPTIONAL.
3. [deployment-systemd-caddy.md](deployment-systemd-caddy.md) — the
   versioned systemd/Caddy/env reference templates and how to install
   them.

## I run a Central deployment

[central.md](central.md) — content publication, Node registration
lifecycle, readiness, safe restart/recovery, HTTPS.

## I run a Node classroom deployment

[node.md](node.md) — package activation/runtime, local offline operation,
the sync worker, safe restart, classroom LAN hostname setup.

## I need to back up or recover a deployment

[backup-restore.md](backup-restore.md) — database dump/restore procedure,
secrets/key backup, and the application-level checks required after a
restore (a successful `pg_restore` alone is not proof of a good restore).

## Sync between Node and Central isn't working

[sync-operations.md](sync-operations.md) — Node provisioning, credential
issue/rotate/revoke, running the sync worker, and how to read
pending/dead-letter state, 400 vs 413 responses, and retry/backoff
behavior.

## Something is broken and I don't know where to start

[troubleshooting.md](troubleshooting.md) — the reference diagnosis order
(`/healthz` → `/readyz` → service status → logs → database → migrations →
profile → package lifecycle → sync credential → Outbox/Inbox state), with
the exact symptoms this repository has actually been built to explain.

## I'm signing off on the Phase 1 release

[phase1-release-checklist.md](phase1-release-checklist.md) — the human
approval checklist. It does not create a git tag; a release/tag remains a
separate, explicit human action.

## Platform policy

Linux is the Phase 1 production/reference release environment. macOS is
supported for local development only — a shipped release-gate run on
macOS fails at the precheck stage by design, and there is no bypass flag
for it.
