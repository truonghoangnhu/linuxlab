# Task 15 / Task 13 Corrective A — Package Operator Surface Design

Date: 2026-09-11
Status: Approved design for implementation planning
Repository: LinuxLab
Task: Task 15 / Task 13 / Corrective A
Start HEAD: `abdda90b2f9f8cbd3942c41ed2d947a42ef4e42b`
Amended: 2026-09-11 — Amendment A (installed-package identity/rollback
alignment), starting HEAD `ffa5c1b165ddbdc90607492c346c36e6936c830d`
Amended: 2026-09-11 — Amendment B (Central publication identity alignment),
starting HEAD `98fc2bddc81fe7a3d4e7e12754062af80feabd81`
Amended: 2026-09-11 — Amendment C (Node package trust anchor), starting
HEAD `69ab51d850b4de1e80746c3f735c70fe151f1f53`

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

### Amendment B

Implementation-plan-time domain inventory found that the Central side of
the spec had the same class of mismatch Amendment A already fixed on the
Node side. `PublicationSnapshot` (like `InstalledPackage`) has no
independent `public_id`; its canonical distributed identity is
`(kind, source_public_id, version)` (ADR-011 pattern). `build_package()`
takes a `PublicationSnapshot` instance, not a publication UUID, and no
single UUID identifying "the publication" exists anywhere in the domain.
The original spec's `--publication <UUID>` argument and `Publication:
<uuid>` output line do not match this reality. Amendment B resolves the
mismatch the same way Amendment A did: by aligning the CLI to the domain's
existing identity instead of inventing a new one.

**RULING B1:** `PublicationSnapshot` has no operator-facing `public_id`.
Its canonical publication identity is `(kind, source_public_id, version)`.

**RATIONALE:** Corrective A is a thin operator adapter and must not
introduce a new publication identity namespace, synthetic UUID, or schema
migration merely for CLI ergonomics.

**RULING B2:** For Phase 1, `learning_path` is the only supported
publication kind (`_SUPPORTED_PUBLICATION_KINDS` in
`linuxlab/publication/package.py` contains exactly one entry:
`"learning_path"`), so the Central CLI exposes `--learning-path` and
`--version` instead of a generic `--kind` / `--source-public-id` /
`--version`.

**RATIONALE:** This reflects the current supported domain and avoids
premature generalization. If additional publication kinds are introduced
later, CLI generalization may be designed then.

### Amendment C

Implementation-plan-time inventory found that `verify_package()` and
`import_package()` both take `trusted_public_key` as a caller-supplied
parameter (`linuxlab/publication/package.py:778`,
`linuxlab/sync/services.py:86`) — neither loads production trust
configuration itself. Central's signing private key already has a
documented production convention (`/etc/linuxlab/keys/package-signing.key`,
per `docs/operator/configuration.md`), but Node has no sanctioned
production source for the matching trusted public key anywhere: not in
settings, not in any operator doc, not in any production code path. The
only place a `trusted_public_key` is ever constructed today is the
Task 15 release-gate test harness's ephemeral, per-run Ed25519 keypair
(`scripts/release_gate/signing_material.py`), which is test/release-gate
scaffolding, never production trust material. Amendment C closes this gap
with the smallest possible addition: one fixed local trust-anchor file and
one loader, not a trust-store subsystem.

**RULING C1:** A production Node trusts exactly one locally pinned Ed25519
public key in Phase 1. Canonical path:
`/etc/linuxlab/keys/package-signing-public.key`.

**RATIONALE:** The package-signing trust model already exists on the
Central side; the missing architectural piece is the production source of
`trusted_public_key` on the Node side. A single pinned local public key is
sufficient for Phase 1 and avoids inventing a trust-store subsystem.

**RULING C2:** The first trusted public key reaches the Node only through
an administrator-controlled out-of-band provisioning channel. It **must
not** be bootstrapped from: the `.llpkg` being verified, Task 14 sync, a
remote fetch, release-gate ephemeral signing material, database state, or
trust-on-first-use.

**RATIONALE:** A package cannot be allowed to choose the trust anchor used
to authenticate itself.

**RULING C3:** Corrective A adds no key-management command, keyring,
revocation system, CA, remote enrollment, or automatic rotation mechanism.

**RATIONALE:** Those are separate security subsystems and are outside the
Phase 1 operator surface.

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
  --learning-path <UUID>
  --version <VERSION>
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
The `PublicationSnapshot` identity and `build_package` contract mismatch
discovered during implementation-plan-time inventory is already resolved
by Amendment B and is not a live mismatch (Section 22).

Identifiers exposed to the operator are one of four distinct layers —
never conflated. See Section 2a for the full identity model; in brief:

- **Authoring source identity** — `LearningPath.public_id`, the stable
  public UUID of the authored source/content lineage. Operator-facing CLI
  field: `--learning-path`.
- **Publication snapshot identity** — the composite tuple
  `(kind, source_public_id, version)`, which identifies one immutable
  publication snapshot. `PublicationSnapshot` has no `public_id` of its
  own. For Phase 1, `kind` is always `"learning_path"` and is implicit;
  the operator supplies `--learning-path` (= `source_public_id`) and
  `--version`.
- **Package/manifest identity** — the manifest's own
  `(package_uuid, package_type, package_version)` fields, which under the
  current package contract mirror the publication snapshot's
  `(source_public_id, kind, version)` exactly. `package_uuid` is not a
  distinct publication identifier — it is `source_public_id`.
- **Installed-artifact identity** — the composite tuple
  `(package_type, source_public_id, package_version)`, which identifies one
  concrete installed immutable artifact on Node. `InstalledPackage` has no
  `public_id` of its own. All three components are required together; none
  of them alone is a stable installed-artifact identity.

Do **not** expose as operator identity:

- a database integer primary key
- a row index
- a filename as identity
- a title or slug
- an ambiguous "latest"
- a synthetic `InstalledPackage` UUID that does not exist in the domain
- a synthetic `PublicationSnapshot` UUID that does not exist in the domain

Filename is transport location only, never identity.

### 2a. Identity Model

Four identity layers, kept distinct throughout this document:

**A. Authoring source** — `LearningPath.public_id` (a real stable UUID
field on `LearningPathPage`). Operator-facing CLI field: `--learning-path`.

**B. Publication snapshot** — canonical identity
`(kind, source_public_id, version)`. No `PublicationSnapshot.public_id`
exists. `kind` is fixed to `"learning_path"` for Phase 1 (the only entry in
`_SUPPORTED_PUBLICATION_KINDS`), so the operator-facing identity reduces to
`--learning-path` (= `source_public_id`) + `--version`.

**C. Package manifest** — current fields, exactly as produced by
`build_package()`'s `_manifest_dict()`:

```
package_uuid    = source_public_id
package_type    = publication kind
package_version = publication version
```

These mirror the publication snapshot's own identity; `package_uuid` is
never a distinct publication UUID.

**D. Node installed artifact** — canonical identity
`(package_type, source_public_id, package_version)`. No
`InstalledPackage.public_id` exists (Amendment A).

Values may mirror one another numerically across these four layers (e.g.
the same `source_public_id` appears in layers A, B, C, and D), but each is
a distinct domain concept owned by a distinct lifecycle stage. A database
primary key never crosses any operator boundary at any layer. Filename is
transport location only, never identity, at any layer.

## 3. Central Publish Semantics

`publish` takes a `LearningPath` stable public identifier.

Flow:

```
--learning-path <UUID>
→ sanctioned read-only LearningPathPage lookup
  (LearningPathPage.objects.get(public_id=<uuid>))
→ publish_learning_path(page, activity_registry)
→ immutable publication snapshot
```

Success output contains only safe stable identifiers that actually exist on
`PublicationSnapshot` (Section 2a, layer B), for example:

```
Operation: publish
Kind: learning_path
LearningPath: <source_public_id>
Version: <version>
```

There is no `Publication: <uuid>` field: `PublicationSnapshot` has no
`public_id` (Amendment B, Ruling B1). `PublicationSnapshot` also has no
separate state field/service contract, so no `State: published` line is
required or invented merely because the operation is named `publish`.

The output must expose `LearningPath` + `Version` together, since that
pair is exactly what the operator needs to supply to a later `build`
invocation.

Rules:

- no direct publication-row mutation from the CLI
- no implicit build
- no invented idempotency behavior beyond what the publication service
  already provides

## 4. Central Build Semantics

`build` takes the canonical publication snapshot identity
(`--learning-path <UUID> --version <VERSION>`, Section 2a layer B) plus a
final output path. There is no single `--publication <UUID>` argument:
`PublicationSnapshot` has no `public_id` (Amendment B, Ruling B1), and
`kind` is implicit (`"learning_path"`, Ruling B2).

Flow:

```
--learning-path <UUID> + --version <VERSION>
→ read-only PublicationSnapshot lookup
  (kind="learning_path", source_public_id=<uuid>, version=<version>)
→ build_package(snapshot, activity_registry, private_key)
→ existing signing boundary
→ immutable .llpkg bytes/artifact
→ safe final file output
```

The lookup requires the exact version supplied by the operator. No
`latest`/`current`/`most_recent` selection semantics are introduced — an
unknown `(source_public_id, version)` pair fails closed rather than
silently selecting a different snapshot.

Rules:

- no implicit publish
- no unsigned fallback
- no private-key CLI argument
- no `--force`
- an existing destination path fails closed
- the parent directory must be valid and writable
- failure must not leave a partial final artifact
- the CLI performs only a read-only `PublicationSnapshot` lookup; it must
  not mutate `PublicationSnapshot` rows
- if the builder returns immutable bytes, the adapter may perform safe
  atomic file I/O around those bytes
- if the repository already has a sanctioned atomic export helper, that
  helper must be reused rather than reimplemented
- `build_package`'s signature (`snapshot, activity_registry, private_key`)
  is not changed merely to accept CLI arguments directly

The CLI must not invent a fabricated package UUID if the manifest uses a
different identity form; the adapter reports whatever stable identity the
manifest actually carries. Under the current package contract, the
manifest's `package_uuid` field equals `source_public_id` — it is not a
distinct publication identifier (Section 2a, layer C).

Success output contains only safe stable identifiers that actually exist,
distinguishing the publication-snapshot identity that was built from the
resulting package-manifest identity, for example:

```
Operation: build
Kind: learning_path
LearningPath: <source_public_id>
Version: <publication version>
PackageUUID: <manifest package_uuid>
PackageType: <manifest package_type>
PackageVersion: <manifest package_version>
Output: <final path>
```

`PackageUUID` is a real manifest field, not an invented one. Under the
current package contract it equals `LearningPath`/`source_public_id` —
the output must not imply it is a second, distinct publication UUID.

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

## 5a. Node Package Trust Anchor

Both `verify` and `import` (Sections 6–7) need a `trusted_public_key` to
call `verify_package()`/`import_package()`. This section defines its
sole production source (Amendment C).

### Trust-anchor file contract

Canonical Node path:

```
/etc/linuxlab/keys/package-signing-public.key
```

Format: PEM `SubjectPublicKeyInfo` containing an Ed25519 public key.

Reference filesystem contract:

- owner: privileged administrator/root
- mode: `0644`
- the LinuxLab service account: read-only

The public key is not confidential; its **integrity** is the security
boundary. The LinuxLab runtime/service account must not be able to modify
or replace the canonical trust-anchor file. Corrective A does not require
application-level UID/GID auditing of the file.

### No configurable trust path in Phase 1

Production commands use the canonical fixed path above. Do **not** add:

- a Django setting to redirect the trust path
- an environment-variable trust path
- `--trusted-key`
- `--public-key`
- `--trust-file`
- `--key-path`

Test code may substitute/monkeypatch the internal path boundary; no
production CLI option may redirect trust to an arbitrary file.

### Loader contract

Logical production loader contract (exact module/function name may be
finalized by implementation planning according to repository ownership
conventions, but this semantic contract is binding):

```
load_package_trusted_public_key() -> Ed25519PublicKey
```

Flow:

```
canonical path
→ read bytes
→ parse PEM public key
→ validate key type
→ return Ed25519PublicKey
```

The loader:

- reads only the canonical trust-anchor file
- parses PEM
- requires Ed25519
- returns a typed public-key object
- fails closed on invalid configuration

The loader must **not**:

- access the database
- access the network
- contact Central
- inspect the package to choose a key
- derive trust from package contents
- generate a key
- use release-gate ephemeral material
- cache trust in the database
- modify the trust-anchor file

### Loader failure semantics

A typed configuration-layer failure concept (e.g.
`PackageTrustConfigurationError`; exact class/module name may follow
current repository conventions during implementation planning) represents
loader-level failure. It must **not** be a Django `CommandError` inside
the low-level loader itself.

Expected loader failures include: file missing, unreadable, path is not a
regular readable file, empty, malformed PEM, or valid PEM but a
non-Ed25519 key.

Boundary:

```
loader typed configuration error
→ management command translates to sanitized CommandError
→ non-zero exit
```

Unexpected programming errors must not be broadly swallowed. Expected
errors must not expose PEM contents, raw key bytes, private signing key
material, secret environment values, or unnecessary parser internals. The
canonical path itself may appear in an actionable error message because it
is not secret.

A valid Ed25519 key that simply does not match the package's signature is
**not** a trust-configuration parse error — it is a package
signature-verification failure (Sections 6–7), reported through
`verify_package`/`import_package`'s own existing error path, not the
loader's.

### No process-global trust cache

The trust anchor is never loaded at module import. Each management-command
invocation obtains the currently provisioned trusted public key when
needed. No process-global persistent trust cache is introduced. A
correctly replaced trust-anchor file therefore takes effect on the next
command invocation without any custom cache-invalidation mechanism.

### Initial provisioning and rotation (conceptual invariants only)

The first trusted public key reaches the Node through an
administrator-controlled provisioning channel, **not** through the package
channel it authenticates:

```
Central signing private key (/etc/linuxlab/keys/package-signing.key)
→ derive matching Ed25519 public key
→ administrator-controlled transfer/provisioning
→ Node (/etc/linuxlab/keys/package-signing-public.key)
```

Corrective A does not implement the transfer/provisioning mechanism.
Corrective C's operator docs will document the operational procedure.

The Node trusted public key must correspond to the Central signing private
key (conceptually `Kpub = public_key(Kpriv)`). Corrective A does not
create an independent Node keypair registered with Central, does not add
a CA/certificate hierarchy, and does not add an export-public-key
management command.

Operational replacement of the trust-anchor file must conceptually be an
atomic, out-of-band administrator action — create a protected temporary
file, validate PEM/Ed25519 type, establish final ownership/mode, then
atomically replace the canonical path — never an in-place edit. The
application loader itself does not perform key replacement; Corrective A
only defines this invariant, and exact shell/operator steps belong to
Corrective C.

Phase 1 trusts exactly one key; there is no multi-key overlap or fallback.
A conceptual rotation sequence (exact operational ordering may be
finalized in Corrective C, as long as package import remains
maintenance-paused during the cutover window): pause package
publication/import; prepare a new Central signing keypair under
administrator control; derive the matching public key; provision the new
public key to intended Nodes; switch Central to the matching private
signing key; perform a package-sign/verify smoke test; resume
publication/import. Corrective A does **not** implement "try new key,
then fall back to old key" — that would be an undeclared multi-key trust
policy.

A suspected signing-key compromise is handled the same way, conceptually:
stop publication/import, replace the Central signing key, derive/provision
a matching Node trusted key, validate the cutover, then resume. Corrective
A does not add a CRL, OCSP, key-status database, trust epochs, package
blacklist, automatic remote revocation, or a remote kill switch.

### Existing installed artifacts are unaffected by rotation

Changing the Node trust anchor affects future package verification/import
only. It does **not** by itself rewrite `InstalledPackage` rows, re-sign
installed packages, modify `previous_package`, activate/archive packages,
re-materialize runtime state, revoke already-installed artifacts, or
trigger background re-verification. Corrective A adds no background
reverification subsystem. Trust-key rotation is not package revocation.

### Multi-Node trust domain

Multiple Nodes belonging to the same Central trust domain may be
provisioned with the same Central public signing key. No Central database
table tracking which Node trusts which key is added; no online lookup is
required; Nodes remain offline-capable.

### Backup/restore (architectural requirement only)

Central's private signing key remains a backup-critical secret. The Node
trusted public key is not secret but should be treated as deployment
configuration necessary to reconstruct the Node's trust domain. The
Central private signing key must never be copied to Node. Corrective A
does not edit operator backup docs; Corrective C will update that
documentation.

### Explicit non-goals (Amendment C)

- multi-key trust store
- simultaneous trusted signers
- key IDs as trust-routing authority
- CA/certificate hierarchy
- trust-on-first-use
- automatic enrollment
- remote trust-anchor fetch
- Task 14 key distribution
- database-backed trust
- package-embedded trust bootstrap
- revocation database
- CRL / OCSP
- package blacklist
- automatic rotation scheduler
- HSM/KMS integration
- per-course signing keys
- per-Node signing keys
- export-public-key management command
- trust/key-management web UI
- new key-management REST API

### Amendment C acceptance

- Node with no trust anchor → verify/import fail closed.
- Node with a malformed/non-Ed25519 trust anchor → fail closed.
- Node trusting public key A + package signed by private key A →
  verify/import succeeds.
- Node trusting public key B + package signed by private key A →
  signature verification fails.
- Changing the trust-anchor file alone causes no `InstalledPackage` state
  mutation.
- Package contents cannot choose or modify the trust anchor.
- Release-gate ephemeral keys are never a production fallback.
- No schema migration. No trust-store subsystem. No new management
  command.

Core invariant: Corrective A may add the missing Node trust-anchor loader,
but it does not become a key-management subsystem.

## 6. Node Verify Semantics

`verify` is read-only.

Flow:

```
capability/profile guard
→ load_package_trusted_public_key() (Section 5a)
→ read file bytes
→ verify_package(package_bytes, activity_registry, trusted_public_key)
→ verified immutable metadata
→ human-readable output
```

Wrong profile/capability fails before the trust anchor is loaded. A
missing or invalid trust anchor produces a sanitized `CommandError`, exits
non-zero, and causes zero database mutation (Section 5a). A valid trust
anchor that simply does not match the package's signature is a normal
signature-verification failure, not a trust-configuration error.

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
capability/profile guard
→ load_package_trusted_public_key() (Section 5a)
→ import_package(package_bytes, activity_registry, trusted_public_key)
→ existing import authority verifies the package internally
→ provenance
→ staged/installed immutable artifact (lifecycle mutation only after
  successful verification)
```

Wrong profile/capability, a missing/invalid trust anchor, a trust anchor
that does not match the package's signature, or an invalid package must
not result in a successful `InstalledPackage` lifecycle mutation.
Transaction authority remains in the existing domain services; the CLI
does not implement repair/rollback through direct ORM manipulation.

The CLI must not trust a previous `verify` invocation: it must not call
`verify_package()` itself and then pass a "verified" flag or cached result
into `import_package()` as a substitute for `import_package()`'s own
mandatory internal verification.

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

Verification (Node trust anchor, Section 5a):

- the command loads the trusted public key only from the fixed canonical
  path (`/etc/linuxlab/keys/package-signing-public.key`); no CLI option
  redirects trust to another file
- the trust anchor is not confidential, but the command never writes,
  replaces, or otherwise mutates it

## 11. Error / Exit Contract

- success: exit 0
- expected operator/domain failure: non-zero via Django management-command
  semantics / `CommandError`

Do **not** invent a separate release-gate-style exit-code table.

Expected failures must be sanitized and actionable, for example:

- LearningPath not found
- publication version not found for LearningPath
- publication snapshot is not buildable
- signing configuration unavailable
- trust anchor missing or malformed (Section 5a)
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
signature requires a model instance rather than the tuple (Section 9).
Likewise, the CLI may resolve `(kind="learning_path", source_public_id,
version)` to a `PublicationSnapshot` instance read-only, for `build_package`
(Section 4). The CLI may **not**:

```
InstalledPackage.objects.update(...)
PublicationSnapshot mutation of any kind
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

Rejection must occur before mutation. For `manage_node_package
verify`/`import` specifically: under the wrong profile/capability, the
Node trust-anchor loader (Section 5a) must not be invoked at all, and
`InstalledPackage` mutation must remain zero. Under the Node
import-capable profile, the trust loader may run.

Do not introduce a direct string check such as `LINUXLAB_PROFILE ==
"node"` for this guard if the repository already defines capability-based
authority (`LINUXLAB_CAN_IMPORT_PACKAGES` / the existing sync capability
helpers in `linuxlab/sync/capabilities.py`) — use that established
authority instead.

## 15. Central Tests

`publish` tests:

- valid `LearningPath` stable public_id → output reports `Kind`,
  `LearningPath`, `Version` (no `Publication` field, no invented `State`)
- unknown UUID
- malformed UUID
- wrong profile
- domain publication rejection

`build` tests, using `--learning-path <UUID> --version <VERSION>`
(Section 2a layer B — there is no `--publication <UUID>` argument):

- valid source + exact version → resolves the correct `PublicationSnapshot`
  → real signed `.llpkg`
- verifier accepts the built artifact
- malformed `LearningPath` UUID → fails before package build/file output
- unknown `LearningPath` UUID → fails
- known `LearningPath` but nonexistent version → fails
- a different, valid version for the same `LearningPath` does not
  silently get selected instead of the requested version
- missing signing configuration → fails closed
- invalid/missing parent directory
- existing destination fails and existing bytes are unchanged
- a failed write/build leaves no final partial artifact
- wrong profile → fails before build/file output

Deterministic bytes are not required unless already a domain invariant.

## 16. Node Verify / Import Tests

### Trust-anchor loader tests

Using temporary test-generated keys/files — never the real production
file under `/etc/linuxlab`:

- valid Ed25519 PEM → returns `Ed25519PublicKey`
- missing path → configuration error
- unreadable path → configuration error
- empty file → configuration error
- malformed PEM → configuration error
- valid RSA public PEM → configuration error
- valid ECDSA public PEM → configuration error
- error output must not contain raw PEM/key sentinel material

### Trust integration tests

**Case A** — Central test private key A signs a package; Node trust
anchor contains matching public key A. `verify`: PASS. `import`: PASS.

**Case B** — Central test private key A signs a package; Node trust
anchor contains a structurally valid Ed25519 public key B. Loader: PASS.
Verification/import: FAIL signature verification. `InstalledPackage`
mutation: zero.

**Case C** — verify a package successfully using key A; replace/tamper
the package bytes; import; import independently re-verifies and FAILs.
This proves a `verify` result is not a trust cache.

### Verify/import command tests

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
- `PublicationSnapshot` rows
- authoring `ActivityVersion` rows
- other lifecycle state

Read-only lookup from the adapter (e.g. resolving the canonical
`(package_type, source_public_id, package_version)` tuple to a model
instance for `activate_package`, or resolving
`(kind="learning_path", source_public_id, version)` to a
`PublicationSnapshot` instance for `build_package`) is acceptable only when
required by an existing service signature. If the service can resolve the
identity itself, that path is preferred.

## 19. Secret-Output Tests

Use test-only sentinel values, never real `.env` secrets, for example:

- `TEST_PRIVATE_KEY_SENTINEL`
- `TEST_SYNC_SECRET_SENTINEL`

Assert these sentinels never appear in stdout or stderr.

Signing failures are specifically exercised for this property.

## 20. CLI-Driven End-to-End Acceptance

One real operator end-to-end scenario. Fixture setup provisions a matching
test Ed25519 keypair: Central's signing boundary uses test private key A
(Section 4), and the Node's trust-anchor path (Section 5a) is
substituted/monkeypatched to point at the corresponding public key A —
never the release-gate's ephemeral keypair reused as a production
stand-in, and never a production `/etc/linuxlab` path touched by a test.

```
Central fixture
→ call_command("manage_central_package", "publish",
               "--learning-path", <learning-path-public-id>)
→ read the returned/reported Version (source_public_id is already known:
  it is the LearningPath public_id used above; kind is implicit
  "learning_path" — there is no separate Publication UUID to capture)
→ call_command("manage_central_package", "build",
               "--learning-path", <learning-path-public-id>,
               "--version", <publication-version>,
               "--output", <path>)
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
prove. The handoff between `publish` and `build` uses only
`LearningPath.public_id` + the reported publication `Version` — never
`PublicationSnapshot`'s database primary key.

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
- a Node trust anchor other than the single fixed local file (Section 5a)
- a remote key-fetch/enrollment mechanism
- multiple simultaneously trusted keys

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

**Resolved by Amendment B:** implementation-plan-time inventory also found
that `PublicationSnapshot` has no `public_id` and that `build_package()`
requires a `PublicationSnapshot` instance rather than a publication UUID.
Amendment B adopted the domain's existing `(kind, source_public_id,
version)` identity as approved architecture (see the Amendment B rulings
above). Implementation must **not** stop merely because:

- `PublicationSnapshot` lacks a `public_id`, or
- `build_package` requires a `PublicationSnapshot` instance rather than a
  single publication UUID.

Those are now intentional, approved architecture, not open mismatches.

**Resolved by Amendment C:** implementation-plan-time inventory also found
that Node has no sanctioned production source for the `trusted_public_key`
that `verify_package()`/`import_package()` require, outside the
release-gate test harness's ephemeral keypair. Amendment C adopted a
single fixed local trust-anchor file
(`/etc/linuxlab/keys/package-signing-public.key`) and a loader contract as
approved architecture (Section 5a). Implementation must **not** stop
merely because:

- no pre-existing Node trust loader exists, or
- `verify_package`/`import_package` require a `trusted_public_key`
  argument that the CLI itself must source.

A small loader implementing the Section 5a contract is now explicitly in
scope.

Implementation must still STOP if it discovers that:

- the `(package_type, source_public_id, package_version)` tuple is not
  actually sufficient/stable for selecting one installed artifact;
- the `(kind, source_public_id, version)` tuple is not actually
  sufficient/stable for selecting one publication snapshot;
- the real supported publication kinds differ materially from
  `{"learning_path"}`;
- `activate_package` requires semantics not representable through current
  domain fields;
- `build_package` requires new package business semantics;
- signing requires architecture redesign;
- the rollback service has materially different behavior from the
  inventory recorded here;
- a schema migration is required;
- package or publication lifecycle semantics must be changed;
- a synthetic publication identity becomes necessary;
- direct ORM mutation would be required;
- a new package state is required;
- verify/import require multiple trusted keys to function correctly;
- the verifier API cannot consume a single Ed25519 public key as designed;
- package trust requires database state;
- the package format must change to carry trust data;
- a remote key-fetch/enrollment mechanism is necessary;
- release-gate ephemeral trust material must be reused in production;
- Task 14 sync must change.

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
9. Is the Node trust anchor loaded only from the fixed canonical path
   (Section 5a), never from the package, the network, the database, or
   release-gate ephemeral material?
10. Does the trust loader fail closed on a missing/malformed/non-Ed25519
    trust anchor, with zero `InstalledPackage` mutation?
11. Is there any production code path that redirects trust to a
    configurable/CLI-supplied file?

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

Amendment C adds its own non-goal list (trust/key-management scope) in
Section 5a — see that section for the full list (multi-key trust store,
CA/certificate hierarchy, trust-on-first-use, remote trust-anchor fetch,
revocation database, HSM/KMS integration, export-public-key command, and
others).

## 27. Acceptance Definition

- Central operator can publish by stable identity (`LearningPath.public_id`).
- Central operator can build an exact immutable publication using the
  canonical publication identity — represented in the Phase 1 CLI by
  `LearningPath.public_id` + publication `Version`, with publication kind
  fixed to `learning_path` — and produce a signed `.llpkg`.
- Corrective A does not add `PublicationSnapshot.public_id` or any
  synthetic publication identity.
- Corrective A does not expose `PublicationSnapshot`'s database primary
  key as an operator identifier.
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
- Node with no trust anchor, or a malformed/non-Ed25519 trust anchor,
  fails verify/import closed.
- Node trusting public key A + package signed by private key A succeeds;
  Node trusting public key B + package signed by private key A fails
  signature verification.
- Changing the trust-anchor file alone causes no `InstalledPackage` state
  mutation.
- Corrective A does not add a trust-store subsystem, a new management
  command, or a configurable trust path.
- A real CLI-driven end-to-end test passes.
- Full regression suite passes.

Core principle: Corrective A adds operator surface, not new package
business semantics.
