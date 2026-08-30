# Task 15 — Phase 1 Release Readiness Gate Design

**Status:** Approved design, pending repository commit and final human spec review
**Date:** 2026-08-31
**Phase:** Phase 1 — Learning Foundation
**Task:** Task 15 — E2E / CI / Operator Docs / Release Readiness Gate
**Previous closed task:** Task 14 — Node → Central Outbox/Inbox Event Sync
**Base HEAD:** `a83b1fb358343394f7e99bd3c9d62838473dea70`

---

## 1. Purpose

Task 15 is the final release-readiness task for Phase 1.

Its purpose is to prove that the Phase 1 architecture works as an integrated system under a production-like Linux runtime, to establish reproducible CI/release gates, and to provide operator documentation and deployment artifacts sufficient to run Central and Node safely.

Task 15 is not a new product feature. It is the integration, operations, recovery, and release gate for the already-built Phase 1 foundation.

When Task 15 receives an independent final PASS:

```text
TASK 15 CLOSED
        ↓
PHASE 1 LEARNING FOUNDATION CLOSED
        ↓
human-controlled RC/tag
        ↓
PHASE 1.5 LABS
```

There is no Task 16 whose sole purpose is to close Phase 1.

---

## 2. Scope

Task 15 contains four bounded areas:

```text
TASK 15
│
├── 1. Release infrastructure
│   ├── /healthz
│   ├── /readyz
│   ├── Gunicorn production reference
│   ├── systemd/Caddy deployment templates
│   └── production configuration/secrets contract
│
├── 2. CI
│   ├── FAST CI
│   └── RELEASE CI
│
├── 3. Phase 1 Release E2E
│   ├── ephemeral Central DB
│   ├── ephemeral Node DB
│   ├── two Gunicorn processes
│   ├── real HTTP
│   ├── signed .llpkg lifecycle
│   ├── learner/runtime workflow
│   ├── Node Outbox → Central Inbox
│   └── backup/restore smoke
│
└── 4. Operator/release documentation
    ├── installation
    ├── Central operations
    ├── Node operations
    ├── deployment
    ├── configuration/secrets
    ├── backup/restore
    ├── sync operations
    ├── troubleshooting
    └── Phase 1 release checklist
```

Task 15 also resolves exactly one accepted Task 14 Minor because it affects operator configuration correctness:

```text
LINUXLAB_SYNC_BACKOFF_BASE_SECONDS
LINUXLAB_SYNC_BACKOFF_CAP_SECONDS
```

The runtime must actually consume these settings before operator documentation advertises them.

The following Task 14 Minors remain deferred backlog and are not Phase 1 closure blockers unless their severity increases during Task 15:

- FileSyncTransport deterministic output;
- durable Central idempotency-conflict audit trail;
- claim-path index / database schema-version hardening.

---

## 3. Explicit Non-Goals

Task 15 does not introduce:

- mandatory Docker or Docker Compose;
- Playwright, Selenium, Chromium, or browser automation;
- a chaos/fault-injection framework;
- a backup daemon, backup scheduler, retention engine, or remote object-storage policy;
- Vault or another external secret manager as a required dependency;
- an automatic Linux installer/provisioner;
- automatic git tagging or release creation;
- a second CI provider;
- a new application runtime architecture;
- Central → Node event synchronization;
- database replication;
- a new learning projection on Central;
- Task 15 work that belongs to Phase 1.5 Labs.

---

## 4. Supported Runtime Contract

### 4.1 Production target

The supported production target for Phase 1 is Linux.

macOS remains supported as a developer workstation environment only and is not a production-supported target.

Linux CI is the release authority.

### 4.2 Database

PostgreSQL 18 is the authoritative database for CI and release validation.

Any test that uses the Django database runs against PostgreSQL, not SQLite.

Pure-Python tests that do not require Django persistence may remain database-free.

### 4.3 Application server

Gunicorn is the Phase 1 production reference server for both Central and Node.

Gunicorn is a deployment reference, not an application architecture dependency.

### 4.4 Process manager

systemd is the Linux reference process manager.

LinuxLab application behavior must not depend on systemd. Other process managers may be used on compatible Linux systems.

### 4.5 Reverse proxy

Caddy is the Phase 1 reference reverse proxy.

Reference topology:

```text
Client
  ↓
Caddy
  ↓ localhost
Gunicorn
  ↓
Django/Wagtail
  ↓
PostgreSQL
```

Central production traffic is HTTPS.

Node classroom traffic may be HTTP or HTTPS depending on local infrastructure, but the reference deployment still places a reverse proxy in front of Gunicorn.

Gunicorn is not exposed directly to the Internet or classroom LAN in the reference deployment.

---

## 5. Production Configuration and Secret Contract

Reference filesystem layout:

```text
/etc/linuxlab/
├── central.env
├── node.env
└── keys/
    └── package-signing.key
```

Sensitive files use restrictive ownership and permissions, with `0600` as the reference mode for environment/secret files.

systemd uses `EnvironmentFile=` to load deployment configuration.

Examples:

```text
EnvironmentFile=/etc/linuxlab/central.env
EnvironmentFile=/etc/linuxlab/node.env
```

Rules:

- repository files never contain production secrets;
- Central signing private keys are outside the repository and outside the database;
- signing private keys are never emitted to application logs or release evidence;
- Node bearer credentials exist in plaintext only on the Node configuration side;
- Central stores only the password hash for Node bearer credentials;
- release evidence never contains DB passwords, bearer credentials, raw secret environment values, or signing private keys.

---

## 6. Health and Readiness Contract

Both Central and Node expose:

```text
GET /healthz
GET /readyz
```

### 6.1 `/healthz`

Purpose: process liveness.

Requirements:

- returns quickly;
- does not query PostgreSQL;
- does not inspect external Internet services;
- does not expose configuration details.

Success response:

```json
{"status":"ok"}
```

### 6.2 `/readyz`

Purpose: service readiness.

It verifies:

- database connectivity;
- migration state compatibility;
- valid deployment profile;
- required configuration for the active profile is present;
- application is ready to receive traffic.

It does not:

- run migrations;
- mutate configuration;
- repair state;
- disclose detailed internal failure data in the HTTP response.

Success:

```json
{"status":"ok"}
```

Failure:

```json
{"status":"not_ready"}
```

Detailed failure diagnostics belong in sanitized internal logs.

The release runner waits on `/readyz`; it does not rely on fixed startup sleeps.

---

## 7. Migration Contract

Database migration is an explicit operator/release step.

Production deployment sequence:

```text
backup
→ python manage.py migrate
→ python manage.py check
→ restart Gunicorn
→ GET /readyz
```

Forbidden behavior:

- migration from Gunicorn/Django application startup;
- `ExecStartPre=python manage.py migrate` in the reference systemd units;
- readiness endpoints that silently mutate schema.

A service with schema/migration mismatch is `not_ready`; startup remains read-only with respect to schema.

---

## 8. CI Architecture

GitHub Actions is the official Phase 1 CI adapter.

CI business logic must remain repository-native. Workflow YAML orchestrates commands but does not reimplement release-gate semantics.

There are two levels.

### 8.1 FAST CI

Reference workflow:

```text
.github/workflows/ci-fast.yml
```

FAST CI runs on pull requests and normal code-change validation.

Required stages:

```text
checkout
→ Python 3.12
→ PostgreSQL 18
→ dependency install
→ ruff check .
→ pytest
→ Central manage.py check
→ Node manage.py check
→ Central makemigrations --check --dry-run
→ Node makemigrations --check --dry-run
```

Django database tests use PostgreSQL.

### 8.2 RELEASE CI

Reference workflow:

```text
.github/workflows/phase1-release-gate.yml
```

RELEASE CI is mandatory:

- when merging to the protected integration/release branch;
- through manual workflow dispatch when an operator needs to reproduce a gate;
- again on the exact commit SHA selected for a release/tag.

The workflow calls the repository-native release runner and uploads artifacts on both PASS and FAIL.

GitHub Actions does not create git tags automatically.

---

## 9. Release Runner as Source of Truth

The release gate source of truth is:

```text
scripts/phase1_release_gate.py
```

Recommended helper decomposition:

```text
scripts/release_gate/
├── environment.py
├── database.py
├── processes.py
├── scenarios.py
├── evidence.py
└── cleanup.py
```

The entrypoint orchestrates helpers.

It does not contain LinuxLab business logic that belongs in application/domain modules.

It must not bypass trust boundaries with raw SQL or direct ORM writes simply to construct a passing E2E state.

Release scenarios use public/domain lifecycle APIs already present in the application.

---

## 10. Ephemeral Release Environment

Each release-gate run has a unique `run_id`.

It creates two isolated PostgreSQL databases:

```text
linuxlab_gate_central_<run_id>
linuxlab_gate_node_<run_id>
```

The runner fails closed if its target resolves to a known developer or production database.

It must not use:

```text
linuxlab_central_dev
linuxlab_node_dev
```

or a configured production database.

Lifecycle:

```text
create databases
→ migrate
→ execute gate
→ cleanup in finally
```

Local debug may use:

```text
--keep-on-failure
```

CI always cleans up ephemeral databases.

---

## 11. Ephemeral Signing Material

Each release-gate run generates a new ephemeral Ed25519 keypair.

Use:

```text
Central → ephemeral private key
Node    → corresponding trusted public key
```

The release evidence may contain:

- algorithm name;
- public-key fingerprint;
- package SHA-256;
- signature verification result.

It must not contain the private key.

The ephemeral private key is deleted during cleanup.

CI therefore does not need the real production signing private key.

---

## 12. Dual-Deployment E2E Topology

Release validation runs Central and Node as two actual deployments:

- separate settings profiles;
- separate PostgreSQL databases;
- separate Gunicorn processes;
- dynamic isolated ports;
- real HTTP connection;
- real `HttpSyncTransport`.

Docker is not required.

Reference flow:

```text
CENTRAL
publish content
    ↓
build signed .llpkg
    ↓
NODE
verify → stage → activate
    ↓
learner lesson/activity
    ↓
Attempt / Result / Progress
    ↓
activity.completed
    ↓
OutboxMessage
    ↓ real HTTP
CENTRAL
InboxMessage durable
```

This gate must exercise deployment boundaries that in-process test-client-only tests can miss.

---

## 13. E2E Business-State Construction

Release scenarios create state through supported service/public workflows.

A test-infrastructure helper may orchestrate those APIs, but it must not use direct ORM/SQL to fabricate trusted business states such as:

```text
InstalledPackage.objects.create(...)
OutboxMessage.objects.create(...)
InboxMessage.objects.create(...)
```

for the purpose of bypassing lifecycle behavior.

The gate validates the boundaries built in Tasks 09–14 rather than only the resulting database shape.

---

## 14. Happy-Path Release Scenario

The required Phase 1 happy path is:

```text
CENTRAL
create learning content/activity
→ publish immutable snapshot
→ build signed .llpkg
→ capture package hash/signature metadata

NODE
verify package
→ stage package
→ activate package
→ serve real learner lesson route
→ serve real activity/runtime route
→ learner submits activity
→ exact ActivityVersion Attempt/Result/Progress is persisted
→ activity.completed is enqueued transactionally

SYNC
Node HttpSyncTransport
→ Central /api/v1/sync/events/
→ Central durable Inbox
→ valid ACK
→ Node Outbox becomes DELIVERED
```

The user-facing surface is exercised over real HTTP routes.

Task 15 does not introduce full browser automation.

---

## 15. Required Negative Scenarios

Exactly five critical negative scenarios are release-blocking in Phase 1.

### 15.1 Invalid `.llpkg` signature

Expected:

- Node rejects the package;
- existing active package remains unchanged;
- no partial trusted inventory state is created.

### 15.2 Invalid or revoked Node credential

Expected:

- Central returns authentication/authorization failure;
- Node Outbox remains retryable/preserved;
- no false `DELIVERED`;
- no credential secret appears in evidence/log output.

### 15.3 ACK loss after durable Central Inbox

Expected:

```text
Central durably stores Inbox
→ ACK is lost
→ Node retries
→ Central returns DUPLICATE
→ exactly one Inbox row exists
→ Node eventually reaches DELIVERED
```

### 15.4 Central unavailable / Node offline operation

Expected:

- learner can continue the local Node learning/runtime flow;
- local grading state remains available;
- Outbox remains durable;
- when Central becomes available, pending events synchronize successfully.

### 15.5 Process restart

Expected:

- active Node package remains usable;
- pending Outbox survives restart;
- application readiness is restored;
- synchronization can resume.

Every scenario must assert both:

1. the expected final state;
2. the forbidden invariant that must not occur.

Example: an invalid package test is incomplete unless it also proves the existing active package was not replaced.

---

## 16. Backup and Restore

Task 15 provides a tested recovery procedure, not a backup service.

Reference database tools:

```text
pg_dump
pg_restore
```

Secrets and signing keys are backed up separately from database dumps.

The default procedure does not bundle secrets and DB dump into one artifact.

### 16.1 Central minimum recoverable state

After restore to a fresh ephemeral database, the gate must prove preservation of:

- published content/snapshot;
- package metadata;
- NodeRegistration;
- durable Inbox/idempotency state.

### 16.2 Node minimum recoverable state

After restore, the gate must prove preservation of:

- active InstalledPackage;
- core learner/runtime state required by Phase 1;
- pending Outbox;
- ability for that pending Outbox to synchronize after recovery.

A successful `pg_restore` command without application-level state verification is not sufficient.

---

## 17. Release Evidence

Each run writes:

```text
artifacts/phase1-release/<run_id>/
├── release-evidence.json
├── release-evidence.md
├── central.log
├── node.log
└── diagnostics/
```

### 17.1 Evidence schema

JSON is schema-versioned from the first release.

Minimum shape:

```json
{
  "schema_version": 1,
  "run_id": "...",
  "git_sha": "...",
  "started_at": "...",
  "finished_at": "...",
  "environment": {
    "python": "...",
    "django": "...",
    "postgresql": "...",
    "platform": "linux"
  },
  "checks": {},
  "scenarios": {},
  "artifacts": {},
  "overall": "PASS"
}
```

Required evidence includes:

- exact git SHA;
- Python/Django/PostgreSQL versions;
- platform;
- Central/Node profile identifiers;
- relevant validation results;
- scenario PASS/FAIL and durations;
- package SHA-256;
- ephemeral signing public-key fingerprint;
- signature verification result;
- Outbox → Inbox proof;
- migration/check status;
- final overall status.

The Markdown evidence is a human-readable projection of the same run.

Release evidence is an audit/reproducibility artifact for a specific commit in a specific environment. It is not a cryptographic certification that all production deployments are identical.

---

## 18. Failure Model

Release runner exit statuses:

```text
0 = PASS
2 = PRECHECK_FAILURE
3 = STARTUP_FAILURE
4 = SCENARIO_FAILURE
5 = CLEANUP_FAILURE
```

Classification:

### PRECHECK_FAILURE
Environment, repository, dependency, database-safety, or required-config conditions prevent a valid run.

### STARTUP_FAILURE
Database migration, Gunicorn startup, or readiness establishment fails.

### SCENARIO_FAILURE
A happy-path, negative-path, recovery, or release invariant fails.

### CLEANUP_FAILURE
Required cleanup cannot be completed.

Runner behavior:

- uses `try/finally`;
- preserves the primary failure classification deterministically;
- records cleanup failures separately if both occur;
- attempts to write evidence and sanitized logs even on failure.

---

## 19. Diagnostic Preservation and Cleanup

On CI PASS or FAIL:

- stop Central and Node Gunicorn;
- drop ephemeral Central and Node databases;
- delete ephemeral signing private key;
- preserve sanitized release evidence and diagnostic logs;
- never preserve plaintext bearer tokens or raw secret environment values.

`--keep-on-failure` is a local-development debugging feature only.

CI does not preserve databases or secret material after failure.

---

## 20. Deployment Artifacts

Version-controlled reference templates:

```text
deploy/
├── systemd/
│   ├── linuxlab-central.service
│   └── linuxlab-node.service
├── caddy/
│   ├── Caddyfile.central.example
│   └── Caddyfile.node.example
└── env/
    ├── central.env.example
    └── node.env.example
```

Requirements:

- no real secrets;
- explicit placeholders;
- correct Central/Node profile configuration;
- Gunicorn bound to a local interface;
- Caddy reverse-proxy contract consistent with the deployment guide;
- Central HTTPS requirement represented;
- no automatic migration in systemd units.

CI performs required static/contract checks on these templates.

If Caddy is available in RELEASE CI, syntax validation may additionally run, but Caddy binary availability is not a FAST CI requirement.

---

## 21. Operator Documentation

Operator documentation lives in:

```text
docs/operator/
├── README.md
├── installation.md
├── central.md
├── node.md
├── configuration.md
├── deployment-systemd-caddy.md
├── backup-restore.md
├── sync-operations.md
├── troubleshooting.md
└── phase1-release-checklist.md
```

### 21.1 README

Single entry point that routes operators by role and task.

### 21.2 installation.md

Documents production prerequisites:

- Linux;
- Python 3.12;
- PostgreSQL 18;
- Gunicorn;
- Caddy;
- systemd as reference.

macOS is explicitly development-only.

### 21.3 central.md

Central operational responsibilities, including publication/package operations, Node registration lifecycle, readiness, and safe restart/recovery.

### 21.4 node.md

Node/offline classroom operations, package activation/runtime, local operation, sync worker, and safe restart.

### 21.5 configuration.md

Classifies configuration as:

```text
COMMON
CENTRAL-ONLY
NODE-ONLY
SECRET
OPTIONAL
```

For each relevant setting, document:

- requirement;
- default when one exists;
- valid profile;
- restart implications;
- secrecy/logging constraints.

### 21.6 deployment-systemd-caddy.md

Explains the versioned templates and the reference topology.

### 21.7 backup-restore.md

Documents database and secret/key backup separately and the application-level verification required after restore.

### 21.8 sync-operations.md

Documents:

- Node provisioning;
- credential issue/rotate/revoke;
- worker execution;
- safe diagnosis of pending/dead-letter states;
- 400/413 operational interpretation;
- retry/backoff behavior.

It must not instruct operators to rewrite trusted Outbox/Inbox records directly with SQL.

### 21.9 troubleshooting.md

Reference diagnosis order:

```text
1. /healthz
2. /readyz
3. systemd service status
4. sanitized application log
5. PostgreSQL connectivity
6. migration state
7. profile/capability
8. package lifecycle
9. sync credential
10. Outbox/Inbox state
```

Example covered symptoms:

- Node learning works but sync does not;
- Central returns 401/403;
- package import fails;
- package signature verification fails;
- `/healthz` succeeds while `/readyz` fails;
- Outbox remains PENDING;
- Node credential is revoked;
- restored DB exists but service remains `not_ready`.

---

## 22. Phase 1 Human Release Checklist

`docs/operator/phase1-release-checklist.md` is the human approval checklist.

Minimum contents:

```text
CODE
[ ] FAST CI PASS
[ ] RELEASE CI PASS
[ ] working tree/release SHA confirmed
[ ] no migration drift

SECURITY
[ ] no secrets in repo/evidence/log
[ ] package signing verified
[ ] Node credential lifecycle verified

CENTRAL/NODE
[ ] both /readyz PASS
[ ] dual-deployment E2E PASS
[ ] offline behavior PASS
[ ] retry/idempotency PASS

RECOVERY
[ ] Central restore smoke PASS
[ ] Node restore smoke PASS

OPERATIONS
[ ] deployment templates reviewed
[ ] operator docs current
[ ] known Minor backlog recorded

EVIDENCE
[ ] release-evidence.json present
[ ] release-evidence.md present
[ ] evidence git_sha == release candidate SHA
```

The checklist does not automatically create a tag.

---

## 23. Sync Backoff Settings Correction

Task 15 closes the accepted Task 14 operator-facing backoff inconsistency.

The runtime must use:

```text
LINUXLAB_SYNC_BACKOFF_BASE_SECONDS
LINUXLAB_SYNC_BACKOFF_CAP_SECONDS
```

instead of dead constants for ordinary transient retry calculations where those settings are intended to apply.

Requirements:

- settings must actually influence runtime behavior;
- tests prove changed settings alter computed retry timing;
- existing T14-D41 protocol-specific long backoff semantics remain intact unless explicitly parameterized by an already-approved setting contract;
- operator docs describe the real runtime behavior;
- no unrelated sync redesign.

---

## 24. Release Gate Trigger Policy

FAST CI is required on PRs.

RELEASE CI is required:

1. when changes enter the protected integration/release branch;
2. before a release/tag;
3. on the exact git SHA intended for release.

A PASS from a different SHA is not release evidence for the selected release commit.

Git tags remain human-controlled.

---

## 25. Release PASS Criteria

Task 15 is PASS only when all of the following are true:

```text
FAST CI PASS
+
RELEASE CI PASS
+
dual-deployment real HTTP E2E PASS
+
5 negative scenarios PASS
+
Central backup/restore smoke PASS
+
Node backup/restore smoke PASS
+
deployment artifacts validated
+
Operator Docs complete
+
release evidence generated
+
independent Task 15 review PASS
```

Task 15 cannot be declared PASS solely because unit/integration tests are green.

---

## 26. Phase 1 Closure Rule

When the independent Task 15 review returns final PASS:

```text
TASK 15 CLOSED
        ↓
PHASE 1 LEARNING FOUNDATION CLOSED
```

The three deferred Task 14 Minor backlog items remain recorded but do not block Phase 1 closure unless Task 15 demonstrates that their severity has materially increased.

The next development phase is Phase 1.5 Labs.

A release candidate or final git tag is created only by explicit human action after the gate and review have passed.

---

## 27. Design Decisions Recorded by Task 15

The implementation should assign sequential Task 15 decision identifiers in the project decision log for the approved choices in this specification.

The approved decisions include:

- Task 15 is the official Phase 1 Release Gate;
- dual real Central/Node deployment over HTTP;
- FAST CI + RELEASE CI;
- only the dead sync-backoff setting Minor enters Task 15;
- repo-native Python release runner as source of truth;
- per-run ephemeral Central/Node databases with fail-closed DB safety;
- release evidence JSON + Markdown, no automatic git tag;
- GitHub Actions as official CI adapter;
- one full happy path + five critical negative scenarios;
- Linux production target, macOS development-only;
- systemd reference, not runtime dependency;
- Gunicorn production reference server;
- Caddy reference reverse proxy;
- `/etc/linuxlab` external config/secrets contract;
- `/healthz` + `/readyz`;
- documented and tested backup/restore, no backup daemon;
- public/domain lifecycle seeding, no trust-boundary bypass;
- ephemeral Ed25519 keypair per release run;
- PostgreSQL-only official CI;
- real HTTP user-surface smoke, no browser automation;
- sanitized diagnostics on failure with cleanup;
- explicit migration step, no startup auto-migrate;
- minimum recoverable business-state restore verification;
- modular operator runbooks;
- versioned deployment templates;
- Gunicorn used in RELEASE CI;
- release gate required on protected-branch integration and exact release SHA.

No implementation-time decision may silently contradict these approved choices. A material new architecture decision requires human approval and a new Task 15 decision entry.

---

## 28. Spec Self-Review

### Placeholder scan
No TBD/TODO placeholders remain.

### Internal consistency
- PostgreSQL 18 is consistently the release database authority.
- Linux is consistently the production target.
- Gunicorn is the runtime server in RELEASE CI and production reference.
- Caddy/systemd are deployment references but not CI runtime dependencies.
- Task 15 does not auto-tag releases.
- Task 15 does not auto-run migrations at application startup.
- Release scenarios use public/domain boundaries.
- Backup/restore is tested but not automated as a scheduled production service.
- T14-D40/T14-D41 behavior is preserved.
- Only the approved sync-backoff Minor is promoted into Task 15.

### Scope check
The task is large but cohesive: every component exists to prove, operate, recover, or release the same Phase 1 system. It remains one release-readiness task rather than multiple independent product subsystems.

### Ambiguity check
Material choices are explicit:
- dual processes and dual databases;
- real HTTP;
- Gunicorn in RELEASE CI;
- no mandatory Docker;
- no browser automation;
- no automatic tag;
- no direct ORM/SQL trust-state fabrication;
- exact five negative scenarios;
- explicit migration;
- Linux release authority;
- human-controlled final release action.

No unresolved architecture choice remains in this design.
