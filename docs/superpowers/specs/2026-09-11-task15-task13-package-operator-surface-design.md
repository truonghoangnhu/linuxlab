# Task 15 / Task 13 Corrective A — Package Operator Surface Design

Date: 2026-09-11
Status: Approved design for implementation planning
Repository: LinuxLab
Task: Task 15 / Task 13 / Corrective A
Start HEAD: `abdda90b2f9f8cbd3942c41ed2d947a42ef4e42b`

## Context

Task 13 independent review found that Phase 1 requires a reproducible
operator-facing package lifecycle, while the repository currently exposes
only internal publication/package/lifecycle services with no operator-facing
entry point.

This corrective adds a thin operator CLI surface over those existing
services. It does **not** add new package business semantics, does not
change existing service contracts, and does not introduce a new
distribution, authorization, or transport subsystem.

This document is an approved design specification for implementation
planning. It is not an implementation plan and it is not code.

## 1. Architecture / Operator Surface

Django management commands are used as thin operator adapters.

Central command:

- `manage_central_package`

Node command:

- `manage_node_package`

Do **not** introduce:

- Wagtail admin actions
- an HTTP operator API
- a new auth model
- a new package subsystem
- remote package push
- automatic Central→Node distribution

Central flow:

```
authoring content
→ publish immutable snapshot
→ build/sign .llpkg
→ operator-selected file output
```

Node flow:

```
.llpkg
→ verify/import
→ staged/installed immutable artifact
→ status
→ activate
→ runtime serves exact ACTIVE artifact
→ rollback to explicit target if required
```

The CLI must never bypass:

- the package verifier
- signature verification
- installed-package lifecycle services
- activation/rollback locking
- immutable publication rules

Profile boundary:

- `manage_central_package`: Central allowed, Node rejected before mutation.
- `manage_node_package`: Node allowed, Central rejected before mutation.

CLI privilege must never imply domain bypass. A command that is invoked with
sufficient OS-level privilege still goes through the same domain services,
the same verification, and the same locking as any other caller.

## 2. Command Set and Argument Contract

Approved command shapes:

Central:

```
manage_central_package publish
  --learning-path <UUID>

manage_central_package build
  --publication <UUID>
  --output <FILE.llpkg>
```

Node:

```
manage_node_package verify
  --file <FILE.llpkg>

manage_node_package import
  --file <FILE.llpkg>

manage_node_package status
  [--installed-package <UUID>]

manage_node_package activate
  --installed-package <UUID>

manage_node_package rollback
  --to-installed-package <UUID>
```

These are approved CLI semantics, but implementation must first inventory
the real service signatures and real domain identifiers (see Section 24).
If an existing sanctioned service uses a materially different stable
identity or contract, implementation must STOP and report the architectural
mismatch rather than force the domain into this CLI shape.

Identifiers exposed to the operator:

- stable public UUID
- immutable package identity/version from the verified manifest

Do **not** expose as operator identity:

- a database integer primary key
- a row index
- a filename as identity
- a title or slug
- an ambiguous "latest"

Filename is transport location only, never identity.

## 3. Central Publish Semantics

`publish` takes a `LearningPath` stable public identifier.

Flow:

```
LearningPath public_id
→ sanctioned publication service
→ immutable publication snapshot
```

Success output contains only safe stable identifiers, for example:

```
Operation: publish
LearningPath: <uuid>
Publication: <uuid>
Version: <version>
State: published
```

Rules:

- no direct publication-row mutation from the CLI
- no implicit build
- no invented idempotency behavior beyond what the publication service
  already provides

## 4. Central Build Semantics

`build` takes an immutable publication identity plus a final output path.

Flow:

```
published immutable publication
→ existing package builder
→ existing signing boundary
→ immutable .llpkg bytes/artifact
→ safe final file output
```

Rules:

- no implicit publish
- no unsigned fallback
- no private-key CLI argument
- no `--force`
- an existing destination path fails closed
- the parent directory must be valid and writable
- failure must not leave a partial final artifact
- if the builder returns immutable bytes, the adapter may perform safe
  atomic file I/O around those bytes
- if the repository already has a sanctioned atomic export helper, that
  helper must be reused rather than reimplemented

The CLI must not invent a fabricated package UUID if the manifest uses a
different identity form; the adapter reports whatever stable identity the
manifest actually carries.

## 5. File Transport Boundary

Corrective A does **not** build a distribution transport subsystem.

Approved flow:

```
Central
→ signed .llpkg
→ operator-approved transfer
→ Node
```

Transfer examples may include USB or secure copy conceptually, but no
transport mechanism is architectural authority in this corrective.

Task 14's Node→Central sync must not be reused as a Central→Node content
channel.

## 6. Node Verify Semantics

`verify` is read-only.

Flow:

```
file bytes
→ existing pure/offline verifier
→ verified immutable metadata
→ human-readable output
```

`verify` must **not**:

- create an `InstalledPackage`
- stage a package
- activate anything
- materialize authoring rows
- create a trusted verification cache

`import` must always verify again independently; a prior `verify` result is
never trusted by `import`.

## 7. Node Import Semantics

`import` reads the file again and calls the existing Node lifecycle import
path.

Flow:

```
file
→ import service
→ internal mandatory verification
→ provenance
→ staged/installed immutable artifact
```

The CLI must not trust a previous `verify` invocation.

Output must distinguish, where applicable:

- manifest/package identity
- Node `InstalledPackage` stable identity
- version
- real lifecycle state

Duplicate-import behavior remains whatever the current lifecycle service
already defines. The CLI must not invent duplicate-import semantics of its
own.

## 8. Node Status Semantics

`status` with no argument gives a deterministic read-only overview
sufficient for an operator to discover `InstalledPackage` stable
identities.

Optional filter:

```
--installed-package <UUID>
```

Output includes:

- `InstalledPackage` stable identity
- package identity
- version
- real lifecycle state

Rules:

- no ORM repr dump
- no "latest" concept
- no mutation

## 9. Activate / Rollback Semantics

`activate`:

```
--installed-package <UUID>
```

Must call the existing lifecycle activation service and preserve:

- completeness checks
- locking
- the single-ACTIVE invariant
- current domain errors

`rollback`:

```
--to-installed-package <UUID>
```

Uses an explicit target. The CLI must not invent "previous" semantics.

If the existing rollback service has a materially different canonical
target contract than an explicit `--to-installed-package` identity,
implementation must STOP and report the mismatch (see Section 24) rather
than reshape the service or the CLI unilaterally.

Rules:

- no direct ORM state transitions
- no retry loop invented in the CLI

## 10. Security / Authorization

Approved security model:

```
OS operator account / sudo policy
→ external /etc/linuxlab configuration
→ deployment profile
→ management command profile guard
→ existing domain service
```

No new Django RBAC model is introduced for this CLI in Phase 1.

Forbidden CLI options:

- `--force`
- `--unsafe`
- `--ignore-state`
- `--skip-lock`
- `--skip-verification`
- `--skip-signature`
- `--allow-revoked`
- `--trust-file`

Signing:

- the private key stays external to Git
- the command does not accept raw private-key material on argv
- the command uses the existing signing configuration/service
- no seed, private key, or raw secret appears in stdout, stderr, or logs

## 11. Error / Exit Contract

- success: exit 0
- expected operator/domain failure: non-zero via Django management-command
  semantics / `CommandError`

Do **not** invent a separate release-gate-style exit-code table.

Expected failures must be sanitized and actionable, for example:

- publication not found
- package signature invalid
- package already active
- activation prerequisite incomplete
- wrong deployment profile
- output already exists

Do **not** catch every `Exception` and hide programming bugs.

No secret-bearing traceback or output for expected failures.

## 12. Transaction / Locking / Idempotency

Transaction authority remains in domain services.

The CLI must not manually create or update lifecycle rows.

Activation/rollback locking remains the existing lifecycle authority.

Idempotency is operation-specific and inherited from current services:

- `verify`: read-only / idempotent
- `status`: read-only
- `build`: the same publication may be buildable again according to
  service semantics, but an existing destination path fails closed
- `import`: duplicate behavior inherited from the lifecycle service
- `activate`: already-active behavior inherited from the lifecycle service
- `rollback`: explicit target; no CLI-generated history semantics

No implicit chaining:

```
publish != publish+build
import != import+activate
build != copy-to-node
rollback != sync
```

## 13. Safe File Output

Approved output invariant:

```
build failure
→ no partial final .llpkg
→ no leaked temp artifact
```

Preferred boundary:

```
build immutable bytes/artifact
→ temp file on same destination filesystem
→ close/fsync as appropriate to existing utilities
→ atomic rename
```

This helper is only implemented if existing package/export utilities do not
already provide the behavior.

The existing output path must fail closed on an existing destination.

## 14. Testing Strategy

Three levels:

A. command contract tests
B. real domain/lifecycle integration tests
C. one CLI-driven end-to-end operator workflow

Command tests cover:

- argument parsing
- malformed UUID
- wrong profile
- expected domain errors
- sanitized output
- no secret output

Profile matrix:

- `manage_central_package`: Central allowed, Node rejected
- `manage_node_package`: Node allowed, Central rejected

Rejection must occur before mutation.

## 15. Central Tests

`publish` tests:

- valid `LearningPath` stable public_id
- unknown UUID
- malformed UUID
- wrong profile
- domain publication rejection

`build` tests:

- valid publication → real signed `.llpkg`
- verifier accepts the built artifact
- unknown publication
- invalid/unpublished publication
- missing signing configuration
- invalid/missing parent directory
- existing destination fails
- original file remains unchanged
- a failed write/build leaves no final partial artifact
- wrong profile

Deterministic bytes are not required unless already a domain invariant.

## 16. Node Verify / Import Tests

`verify`:

- valid package → success, zero `InstalledPackage` mutation, zero
  activation/staging mutation
- tampered/invalid package → failure, zero mutation

`import`:

- use a real valid signed `.llpkg`
- must prove import performs its own verification

Adversarial test:

```
verify valid file A
→ alter/replace A
→ import A
→ import must fail
```

This locks the invariant: a `verify` result is never a trusted cache.

## 17. Node Status / Activate / Rollback Tests

`status`:

- no-arg listing
- filtered by stable UUID
- deterministic output
- real state/version
- unknown identity behavior

`activate`/`rollback` integration:

```
STAGED A
→ activate A
→ A ACTIVE

STAGED B
→ activate B
→ B ACTIVE

rollback explicitly to A
→ A ACTIVE
```

After transitions: at most one ACTIVE for the relevant source/type
according to the existing lifecycle invariant.

Existing lifecycle concurrency tests are not duplicated if the lifecycle
already owns them.

## 18. Service Boundary Test / Review Invariant

Command adapters must not directly mutate:

- `InstalledPackage`
- publication snapshot rows
- authoring `ActivityVersion` rows
- other lifecycle state

Read-only stable-ID lookup from the adapter is acceptable only when required
by an existing service signature. If the service can resolve the UUID
itself, that path is preferred.

## 19. Secret-Output Tests

Use test-only sentinel values, never real `.env` secrets, for example:

- `TEST_PRIVATE_KEY_SENTINEL`
- `TEST_SYNC_SECRET_SENTINEL`

Assert these sentinels never appear in stdout or stderr.

Signing failures are specifically exercised for this property.

## 20. CLI-Driven End-to-End Acceptance

One real operator end-to-end scenario:

```
Central fixture
→ call_command publish
→ capture publication identity
→ call_command build
→ .llpkg exists
→ pure file boundary
→ Node call_command verify
→ assert no mutation
→ Node call_command import
→ Node call_command status
→ Node call_command activate
→ lifecycle/runtime confirms ACTIVE
→ Node call_command rollback
→ previous explicit target ACTIVE again
```

After minimal fixture setup, package-lifecycle steps under test must go
through management commands. Internal services are not called directly for
the same steps whose operator accessibility the end-to-end test is meant to
prove.

Central and Node DB/profile separation must remain intact. No DB
replication or row copying.

## 21. Regression / Acceptance Boundaries

Corrective A must not change:

- the signed `.llpkg` format
- the verifier trust boundary
- Node installed lifecycle semantics
- offline runtime semantics
- Task 14 Node→Central sync
- release-gate invariants
- deployment templates
- Task 13 docs (deferred to Corrective C)
- the database schema

No migration is expected.

If implementation requires:

- a schema migration
- a new package state
- a major domain-service signature change
- a new auth model
- an HTTP API
- direct lifecycle ORM mutation

implementation must STOP and return to architecture review.

## 22. Required Implementation-Time Inventory

Before writing code, implementation must inspect the real signatures and
contracts for:

- `publish_learning_path`
- `build_package`
- the package verifier
- `import_package`
- `activate_package`
- `rollback_package`
- the `InstalledPackage` read/query model

If the CLI→service mapping remains a thin adapter over these as they exist
today: proceed.

If it does not: STOP and report the mismatch rather than reshaping either
side to force a fit.

## 23. Required Verification

Corrective A verification must include at least:

- new package-operator tests
- relevant publication/package tests
- relevant Node lifecycle tests
- relevant sync/runtime regression tests as appropriate
- the scripts suite
- full repository pytest
- Ruff
- Central `manage.py check`
- Node `manage.py check`
- Central migration drift check
- Node migration drift check

The full suite is mandatory because this operator surface crosses
publication, signing, package verification, Node import, activation, and
rollback boundaries.

## 24. Independent Review Acceptance Questions

The future independent reviewer must answer:

1. Are the management commands truly thin adapters?
2. Is there any bypass around verifier/signature/lifecycle lock?
3. Does wrong-profile usage fail before mutation?
4. Is build output fail-closed, atomic, and non-overwriting?
5. Is verify truly read-only?
6. Does import verify independently?
7. Do activate/rollback preserve the single-ACTIVE invariant?
8. Does the end-to-end test prove a real operator workflow via CLI rather
   than replacing CLI steps with direct internal service calls?

Corrective A closes only with no valid Critical or Important finding.

## 25. Task 13 Integration Sequence

Approved sequence:

```
Corrective A
Package Operator Surface
→ independent review A

Corrective B
Readiness Consistency
→ independent review

Corrective C
Task 13 docs + contract tests
→ independent Task 13 review

then:

Task 13 CLOSED
→ Task 14 UNBLOCKED
```

Task 13 must **not** be closed immediately after Corrective A.

## 26. Explicit Non-Goals

- JSON output mode
- bulk import
- bulk activation
- network auto-discovery
- Central→Node auto-copy
- progress bars
- interactive prompts
- `--force` / `--latest` / `--yes`
- package deletion/purge
- admin GUI
- HTTP operator API
- scheduled activation
- remote Node management
- new package-distribution protocol
- new audit subsystem
- new Django operator permission model

## 27. Acceptance Definition

- Central operator can publish by stable identity.
- Central operator can build a signed `.llpkg`.
- Package can cross a pure file boundary.
- Node operator can verify without mutation.
- Node operator can import through verifier/lifecycle authority.
- Node operator can discover installed stable identities.
- Node operator can activate explicitly.
- Node operator can rollback to an explicit target.
- Wrong-profile use fails closed.
- No command exposes secrets.
- No command offers bypass/force behavior.
- No direct lifecycle ORM mutation is introduced.
- No schema migration is required.
- A real CLI-driven end-to-end test passes.
- Full regression suite passes.

Core principle: Corrective A adds operator surface, not new package
business semantics.
