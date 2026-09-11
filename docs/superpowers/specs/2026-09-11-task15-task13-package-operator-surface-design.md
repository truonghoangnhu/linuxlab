# Task 15 / Task 13 Corrective A — Package Operator Surface Design

Date: 2026-09-11
Status: Approved design for implementation planning
Repository: LinuxLab
Task: Task 15 / Task 13 / Corrective A
Start HEAD: `abdda90b2f9f8cbd3942c41ed2d947a42ef4e42b`
Amended: 2026-09-11 — Amendment A (installed-package identity/rollback
alignment), starting HEAD `ffa5c1b165ddbdc90607492c346c36e6936c830d`

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

### Amendment A

Implementation-time domain inventory (Section 22) confirmed that
`InstalledPackage` has no independent `public_id` and that
`rollback_package()` does not accept an explicit target. The original
spec's `--installed-package <UUID>` / `--to-installed-package <UUID>`
contract does not match this reality. Amendment A resolves the mismatch by
aligning the CLI to the domain's existing identity and rollback semantics
instead of introducing a new identity or new rollback contract.

**RULING:** Use the existing `InstalledPackage` composite identity —
`(package_type, source_public_id, package_version)` — as the operator-facing
installed-artifact identity. No `InstalledPackage.public_id` field and no
synthetic installed-package UUID are introduced.

**RATIONALE:** Corrective A is a thin operator adapter and must not create a
new identity namespace or schema migration merely for CLI ergonomics.

**RULING:** `rollback` exposes the existing
`rollback_package(package_type, source_public_id)` semantics — rollback to
the currently active package's recorded `previous_package`, chosen entirely
by the existing lifecycle service. No explicit-target rollback argument is
introduced.

**RATIONALE:** Explicit-target rollback would constitute new lifecycle
business semantics and is outside Corrective A.

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
→ rollback via the existing rollback authority if required
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
  [--package-type <TYPE>]
  [--source-public-id <UUID>]
  [--package-version <VERSION>]

manage_node_package activate
  --package-type <TYPE>
  --source-public-id <UUID>
  --package-version <VERSION>

manage_node_package rollback
  --package-type <TYPE>
  --source-public-id <UUID>
```

These are approved CLI semantics, but implementation must first inventory
the real service signatures and real domain identifiers (see Section 22).
If an existing sanctioned service uses a materially different stable
identity or contract than documented here, implementation must STOP and
report the architectural mismatch rather than force the domain into this
CLI shape. The `InstalledPackage` identity and `rollback_package` contract
mismatch discovered during Corrective A's own implementation-time inventory
is already resolved by Amendment A and is not a live mismatch (Section 22).

Identifiers exposed to the operator are one of three distinct concepts —
never conflated:

- **Source identity** — `source_public_id`, the stable public UUID of the
  packaged source/content lineage.
- **Installed-artifact identity** — the composite tuple
  `(package_type, source_public_id, package_version)`, which identifies one
  concrete installed immutable artifact on Node. All three components are
  required together; none of them alone is a stable installed-artifact
  identity.
- **Package/manifest identity** — whatever immutable identity/version the
  verified manifest actually carries. This is not assumed to be the same as
  the installed-artifact identity above.

`LearningPath` and publication `public_id` values (Sections 3–4) are
unrelated, legitimate stable identifiers and are unaffected by this
distinction.

Do **not** expose as operator identity:

- a database integer primary key
- a row index
- a filename as identity
- a title or slug
- an ambiguous "latest"
- a synthetic `InstalledPackage` UUID that does not exist in the domain

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

Output must expose the canonical installed-artifact identity so the
operator can copy it into a later `status`/`activate`/`rollback` invocation,
for example semantically:

```
Operation: import
PackageType: <package_type>
Source: <source_public_id>
Version: <package_version>
State: <real lifecycle state>
```

If the verified manifest carries an additional distinct package/manifest
identity, it may also be shown, but the CLI must not invent one. There is
no `InstalledPackage.public_id` to report — see the identifier contract in
Section 2.

Duplicate-import behavior remains whatever the current lifecycle service
already defines. The CLI must not invent duplicate-import semantics of its
own.

## 8. Node Status Semantics

`status` with no argument gives a deterministic read-only overview
sufficient for an operator to discover installed-artifact canonical
identities.

Optional filters, using the canonical identity fields (Section 2):

```
--package-type <TYPE>
--source-public-id <UUID>
--package-version <VERSION>
```

Output includes, per installed artifact:

- `package_type`
- `source_public_id`
- `package_version`
- real lifecycle state

Rules:

- no ORM repr dump
- no "latest" concept
- no mutation
- no database primary key
- no `InstalledPackage` UUID/public_id, which does not exist (Section 2)

## 9. Activate / Rollback Semantics

`activate`:

```
--package-type <TYPE>
--source-public-id <UUID>
--package-version <VERSION>
```

Activation selects a concrete installed artifact using this canonical
tuple:

```
(package_type, source_public_id, package_version)
→ sanctioned read-only lookup (the existing activation service takes an
  InstalledPackage instance, not the tuple, so the adapter resolves it
  first)
→ existing lifecycle activation service
→ existing lock/completeness checks
→ resulting ACTIVE state
```

The adapter's lookup is read-only; it must not mutate lifecycle state
itself. `activate_package` is not redesigned merely to make the CLI
signature take the tuple directly.

Must preserve:

- completeness checks
- locking
- the single-ACTIVE invariant
- current domain errors

`rollback`:

```
--package-type <TYPE>
--source-public-id <UUID>
```

Exposes the existing `rollback_package(package_type, source_public_id)`
lifecycle contract exactly as it exists today: rollback to the currently
active package's recorded previous package for that
`(package_type, source_public_id)` scope, chosen entirely by the existing
lifecycle service. The command reports the resulting active artifact's
identity/state.

The CLI must **not** invent:

- explicit historical target selection
- "previous" selection rules of its own
- version-order semantics
- rollback stack/history semantics

If the existing rollback service rejects rollback because no active
package or no recorded previous package exists, that sanctioned domain
failure is propagated as-is.

If a future inventory finds the rollback service's actual behavior
materially different from this description, implementation must STOP and
report the mismatch (see Section 22) rather than reshape the service or the
CLI unilaterally.

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
- `rollback`: behavior is entirely inherited from the existing
  `rollback_package` service; the CLI adds no version-selection policy of
  its own

The CLI may resolve a canonical `(package_type, source_public_id,
package_version)` tuple to a model instance read-only, when a service
signature requires a model instance rather than the tuple (Section 9). The
CLI may **not**:

```
InstalledPackage.objects.update(...)
manually archive an ACTIVE package
manually choose a rollback target
manually activate a historical version
```

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
- filtered by the canonical `(package_type, source_public_id,
  package_version)` fields
- deterministic output
- real state/version
- unknown identity behavior

`activate`/`rollback` integration, using the current lifecycle service's
own rollback contract rather than an arbitrary explicit version target:

```
STAGED A
→ activate A (package_type, source_public_id, version A)
→ A ACTIVE

STAGED B (same package_type/source_public_id, version B)
→ activate B
→ B ACTIVE, A ARCHIVED, B.previous_package == A

rollback for (package_type, source_public_id)
→ existing rollback_package() selects A (B's recorded previous package)
→ A ACTIVE again
```

After transitions: at most one ACTIVE for the relevant source/type
according to the existing lifecycle invariant. The test must not assume an
arbitrary explicit version target unless that is already the lifecycle
contract — here the target is whichever package `rollback_package()`
determines, not an operator-chosen version.

Existing lifecycle concurrency tests are not duplicated if the lifecycle
already owns them.

## 18. Service Boundary Test / Review Invariant

Command adapters must not directly mutate:

- `InstalledPackage`
- publication snapshot rows
- authoring `ActivityVersion` rows
- other lifecycle state

Read-only lookup from the adapter (e.g. resolving the canonical
`(package_type, source_public_id, package_version)` tuple to a model
instance for `activate_package`) is acceptable only when required by an
existing service signature. If the service can resolve the identity itself,
that path is preferred.

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
→ capture the canonical installed tuple
  (package_type, source_public_id, package_version)
→ Node call_command status
→ Node call_command activate using the canonical tuple
→ lifecycle/runtime confirms ACTIVE
→ establish another lifecycle version as required by existing tests/domain
→ Node call_command rollback using package_type + source_public_id
→ existing rollback authority selects the result
→ command reports the resulting active tuple/state
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

**Resolved by Amendment A:** this inventory already ran once for
Corrective A and found that `InstalledPackage` has no `public_id` and that
`rollback_package(package_type, source_public_id)` does not accept an
explicit target. Amendment A adopted the domain's existing identity and
rollback contract as approved architecture (see the rulings above).
Implementation must **not** stop merely because:

- `InstalledPackage` lacks a `public_id`, or
- `rollback_package` does not accept an explicit target UUID.

Those are now intentional, approved architecture, not open mismatches.

Implementation must still STOP if it discovers that:

- the `(package_type, source_public_id, package_version)` tuple is not
  actually sufficient/stable for selecting one installed artifact;
- `activate_package` requires semantics not representable through current
  domain fields;
- the rollback service has materially different behavior from the
  inventory recorded here;
- a schema migration is required;
- package lifecycle semantics must be changed;
- direct ORM mutation would be required;
- a new package state is required.

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
- Node operator can discover canonical installed-artifact identities
  composed of `package_type`, `source_public_id`, and `package_version`.
- Node operator can activate explicitly using that canonical identity.
- Node operator can invoke the canonical rollback operation through the
  existing lifecycle authority and obtain the resulting active package
  identity/state.
- Wrong-profile use fails closed.
- No command exposes secrets.
- No command offers bypass/force behavior.
- No direct lifecycle ORM mutation is introduced.
- No schema migration is required.
- Corrective A does not add `InstalledPackage.public_id` or any synthetic
  installed identity.
- Corrective A does not change rollback target-selection semantics.
- A real CLI-driven end-to-end test passes.
- Full regression suite passes.

Core principle: Corrective A adds operator surface, not new package
business semantics.
