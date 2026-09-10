# Task 15 Task 11 Corrective v14 Design

Date: 2026-09-10
Status: Approved design, pending repository commit and human review of the written spec
Repository: LinuxLab
Task: Task 15 / Task 11 / Corrective v14
Base candidate before v14: `65c579a45e07d867a3cbabb69b1615e09bf376ea`

## 1. Purpose

Corrective v14 closes the remaining release-readiness boundary defects found by the independent v13 review without redesigning the release-gate subsystem or starting Task 15 Task 12.

The v13 implementation demonstrated that the normal seven-scenario release gate can pass, but the review found adversarial states that the authority layer still accepts. Those states are unacceptable because Task 15 Task 11 is intended to produce fail-closed terminal truth, stable resource ownership, and independently validated release evidence.

Corrective v14 is therefore a strict boundary closure. It must close every valid Critical and Important finding from the v13 review while preserving all v7-v13 invariants already demonstrated by regression tests and real lifecycle runs.

## 2. Scope

Corrective v14 is limited to these six boundaries:

1. Terminal result coherence in `GateRunResult`.
2. Exact semantic validation in `validate_for_pass()`, especially UUID proof typing.
3. Remaining constructor sealing for terminal or authority-producing dataclasses.
4. Stable-ID-only structural ownership in `ResourceLedger`.
5. Atomic and graph-safe resource relocation.
6. Symmetric fail-closed freeze/thaw/export validation.

A small test-only cleanup for existing temporary-directory leakage is permitted if it is local to an already touched test file and introduces no new framework or helper subsystem.

## 3. Non-goals and hard stops

Corrective v14 must not:

- change database schema or create migrations;
- change the `.llpkg` package format;
- change the Task 14 sync protocol;
- change Activity, Attempt, grading, or installed-artifact architecture;
- add a serializer framework;
- add a new ResourceGraph subsystem;
- add a macOS bypass flag to the shipped release-gate CLI;
- redesign the release-evidence JSON or Markdown schema;
- refactor unrelated modules;
- start Task 15 Task 12.

If implementation reveals that a valid blocker requires work outside these boundaries, implementation must stop and return to architectural review instead of expanding scope silently.

## 4. Governing principles

### 4.1 Canonical boundary principle

Canonical boundaries validate representation; they do not manufacture canonicality.

In particular, release-critical constructors and validators must not coerce arbitrary values into accepted forms by calling `str(value)`, `int(value)`, `bool(value)`, `Path(value)`, `UUID(str(value))`, `Enum.value`, or equivalent normalization inside the authority boundary.

Canonicalization must happen before terminal object construction at the domain-producing call site that owns the conversion.

### 4.2 Terminal truth principle

`GateRunResult` is terminal truth, not a loosely typed transport object. Its constructor must reject internally contradictory combinations even if the normal runner does not currently produce them.

### 4.3 Structural ownership principle

A locator answers where a resource is. A `resource_id` identifies which resource it is. A `parent_resource_id` identifies who structurally owns it.

Locator containment, path prefixes, and legacy parent locator tuples must not override structural ownership after registration.

### 4.4 Evidence algebra principle

Freeze and thaw/export are two directions over the same canonical terminal algebra. Both directions must fail closed on noncanonical keys or values.

### 4.5 Evidence-before-PASS principle

A PASS result must be backed by a coherent, published, independently PASS-eligible `ReleaseEvidence` instance. No alternate or reconstructed authority may supersede that object.

## 5. Terminal authority design

### 5.1 `GateRunResult` becomes a coherence gate

Keep the existing `GateRunResult` type. Do not introduce a state-machine abstraction.

Its constructor or `__post_init__()` must enforce relational coherence in addition to exact field types.

For `overall == "PASS"`, all of the following must hold simultaneously:

- `overall` is exact builtin `str` with value `"PASS"`;
- `exit_code` is exact builtin `int` with value `0`;
- `stage` is the terminal `DONE` value required by the existing contract;
- `evidence` is an exact `ReleaseEvidence` instance, never a subclass or proxy;
- `terminal_failure is None`;
- `evidence.overall == "PASS"`;
- `evidence.exit_code == 0`;
- `evidence.primary_failure is None`;
- `evidence.cleanup_failure is None`;
- `evidence.finalization_failure is None`;
- `evidence.cleanup_status` is the existing clean terminal value;
- `evidence.retention["execution_resources_retained"] is False`;
- `validate_for_pass(evidence).eligible is True`;
- the publication path set is present and structurally coherent.

These constructions must therefore fail:

- PASS with `evidence=None`;
- PASS wrapping evidence whose `overall` is a failure;
- PASS wrapping evidence whose `exit_code` is nonzero;
- PASS wrapping evidence with cleanup, primary, or finalization failure;
- PASS wrapping evidence with retained execution resources;
- PASS with unpublished or partial publication paths.

For non-PASS results, v14 must not force every failure mode into one shape because precheck, startup, scenario, cleanup, finalization, and emergency paths intentionally differ. It must still enforce these relational invariants:

- result `overall` and `exit_code` obey the existing exit contract;
- if evidence is present, result `overall` and `exit_code` agree with evidence;
- publication paths cannot exist if evidence is absent;
- publication paths are all-or-none;
- no contradictory terminal authority is accepted.

The existing exit contract remains:

- 0 = PASS
- 2 = PRECHECK_FAILURE
- 3 = STARTUP_FAILURE
- 4 = SCENARIO_FAILURE
- 5 = CLEANUP_FAILURE

### 5.2 Publication path representation

Terminal path fields remain exact builtin `str | None` as decided in v13.

Internal publication code may use `pathlib.Path`, but conversion to canonical strings must occur before `GateRunResult` construction.

For a published result:

- `evidence_dir`, `json_path`, and `markdown_path` are all present;
- JSON and Markdown paths are structurally under the evidence directory;
- expected terminal filenames remain consistent with the current publication contract.

The constructor must not depend on current filesystem existence. Physical publication and atomicity belong to the publisher; the terminal dataclass verifies structural consistency only.

### 5.3 Exact proof validation

`validate_for_pass()` must independently revalidate canonical proof representation.

For `event_uuid`:

1. first require `type(value) is str`;
2. then parse/validate the UUID string according to the existing canonical UUID format;
3. reject raw `uuid.UUID`, integers, string subclasses, bytes, `Path`, enums, and opaque objects before parsing.

`UUID(str(value))` or equivalent coercion is forbidden at this boundary.

The final PASS validator remains independent even though constructors are sealed. It must not merely assume constructor validation has already happened.

### 5.4 Remaining constructor sealing

`SignatureProof` is explicitly in v14 scope. Every boolean field must use exact builtin boolean semantics, not permissive `isinstance()` logic. String and optional-string fields must follow their exact existing schema.

`Authority` is also in v14 scope because it directly participates in constructing `run["authority"]` and therefore belongs to the authority-producing chain. Its fields must be constructor-sealed according to the current schema:

- authoritative flag: exact builtin `bool`;
- platform: exact builtin `str`;
- reason: exact builtin `str | None`.

No scalar subclasses, raw UUIDs, paths, bytes, enums, opaque objects, or caller-owned mutable containers may survive by identity into terminal authority.

Evidence assembly must construct an explicit canonical mapping from validated `Authority` fields rather than treating `authority.__dict__` as a canonicalization mechanism.

Existing sealing for `ReleaseEvidence`, `FailureRecord`, `ScenarioResult`, `PassDecision`, and `OutboxInboxProof` must remain closed.

## 6. ResourceLedger design

### 6.1 `parent_resource_id` is the sole structural authority

After acquisition, authoritative resource structure is:

- `resource_id`: identity;
- `parent_resource_id`: structural ownership;
- `kind`: resource classification;
- `safe_id`: locator only.

`children_of(parent_id)` must determine children only from `ref.parent_resource_id == parent_id`.

Release-critical traversal and reconciliation must not use:

- `safe_id.startswith(...)`;
- path containment;
- `Path.is_relative_to(...)`;
- `commonpath(...)`;
- `(parent_kind, parent_safe_id)` matching;
- any other locator-derived ownership inference.

Legacy `(parent_kind, parent_safe_id)` may remain only as compatibility, recovery, or diagnostic metadata. It must not determine traversal, reconciliation, release, retention, or PASS eligibility.

### 6.2 Remove containment fallback from authoritative reconciliation

`reconcile_verified_parent_removal(parent_id, ...)` must traverse only the descendants linked to that parent through `parent_resource_id`.

If a resource is explicitly parented to Q but its locator happens to be physically underneath P, reconciling P must not release it.

If a resource is unparented but its locator lies underneath P, reconciling P must not infer ownership and auto-release it.

When ownership is uncertain, fail closed:

- retain the ledger record;
- report retention truthfully if it is an execution-class resource;
- never infer enough structure from locator containment to permit false release or false PASS.

### 6.3 Legacy parent locator resolution occurs only at acquisition

For compatibility, `acquire()` may continue accepting legacy `parent_kind` and `parent_safe_id` arguments where existing call sites still require them.

Those values may be used only to resolve an active parent exactly once at acquisition. The resulting stable `parent_resource_id` becomes authoritative immediately.

If a supplied legacy parent pair cannot resolve uniquely to an active parent, acquisition must fail closed rather than creating a half-parented record.

New or migrated production call sites should pass `parent_resource_id` directly where practical.

### 6.4 Relocation and reparenting are separate operations

`relocate()` changes only locator state:

- `resource_id`: unchanged;
- `safe_id`: old locator to new locator;
- `parent_resource_id`: unchanged.

Relocation must never re-resolve or modify structural parentage from `parent_kind` or `parent_safe_id`.

`set_parent(child_id, parent_id)` is the only operation that changes structural parentage.

If the legacy `relocate()` signature currently permits parent arguments, v14 should migrate real production call sites and then either reject structural parent changes through `relocate()` or treat legacy metadata as non-authoritative diagnostic data only. Silent reparenting is forbidden.

### 6.5 Relocation must be atomic in the in-memory ledger

Before any locator index or record mutation, validate all preconditions:

- source resource exists and is active;
- new locator is valid under the existing locator rules;
- `(kind, new_safe_id)` is not already owned by another active resource;
- old locator equals new locator only through a safe no-op path;
- stable `resource_id` remains unchanged;
- parent graph is not mutated;
- locator indexes remain one-to-one and internally consistent.

If relocation fails, the ledger must remain exactly as it was before the call.

A collision such as moving A onto B's active locator must raise `ResourceOwnershipError` before any destructive index update.

### 6.6 `set_parent()` remains the only graph mutation API

Existing v13 invariants remain:

- unknown child rejected;
- unknown parent rejected;
- self-parent rejected;
- graph cycle rejected;
- `parent=None` detaches legally.

All validations must complete before graph mutation. A rejected reparenting attempt must leave the previous valid graph unchanged.

### 6.7 Parent relocation lifecycle

The following must remain valid:

1. acquire parent P;
2. acquire child C under P;
3. relocate P to a new locator;
4. P retains the same `resource_id`;
5. C retains `parent_resource_id == P.resource_id`;
6. `children_of(P.resource_id)` still returns C;
7. physically remove the relocated parent;
8. reconcile using `P.resource_id`;
9. release descendants only when each descendant's own locator is independently verified absent;
10. keep physically surviving descendants owned, which must prevent PASS if they are execution resources.

### 6.8 Same-prefix siblings

Path prefix similarity has no ownership meaning.

For example, `/tmp/run` must not own `/tmp/run-outside/file.dump` unless an explicit structural parent relation says so. Removing or reconciling `/tmp/run` must leave the sibling physically present and ledger-owned.

### 6.9 Partial dump identity

The dump resource must be reserved before the producer begins. The same stable `resource_id` assigned at reservation must remain associated with any partially created output through producer failure and cleanup.

No release/reacquire identity substitution is allowed.

This invariant applies to both Central and Node dump lifecycles where those paths are produced.

## 7. Freeze/thaw/export design

### 7.1 One canonical algebra

The supported frozen terminal algebra remains:

- exact builtin `str`;
- exact builtin `int`;
- exact builtin `float`;
- exact builtin `bool`;
- `None`;
- `MappingProxyType` with exact builtin `str` keys and canonical values;
- tuple of canonical values;
- frozenset of canonical values.

Where a schema field requires a specific numeric type, the exact schema type applies. For example, `bool` is never valid where an exact `int` is required.

Unsupported values include:

- bytes and bytearray;
- `Path` or other `os.PathLike` objects;
- UUID objects;
- Enum instances;
- scalar subclasses;
- opaque custom objects;
- custom mapping keys;
- noncanonical mapping keys.

### 7.2 Freeze and thaw are symmetric fail-closed boundaries

`deep_freeze()` must continue validating exact canonical mapping keys and values.

`_thaw()` must independently validate the same canonical key/value constraints. It must not assume the input necessarily came from `deep_freeze()`.

For every frozen mapping, `_thaw()` must require `type(key) is str` before exporting the key.

A crafted mapping such as `MappingProxyType({EvilStr("x"): 1})` must raise `UnsupportedEvidenceValueError`.

No unconditional fallback may return arbitrary unsupported objects unchanged.

### 7.3 No coercion inside freeze or thaw

Neither `deep_freeze()` nor `_thaw()` may convert unsupported types into canonical representations. Canonical conversion must happen before the terminal/evidence boundary.

### 7.4 `ReleaseEvidence.to_dict()` stays fresh and detached

`to_dict()` must produce a fresh plain-data tree consisting only of:

- builtin dict;
- builtin list where the current export contract requires list-like output;
- exact builtin scalar leaves;
- `None`.

Mutating the returned structure must not alter:

- the frozen evidence snapshot;
- persisted JSON;
- persisted Markdown;
- `GateRunResult.evidence`.

## 8. Publication truth design

### 8.1 One evidence instance is authoritative

JSON and Markdown must both be rendered from the same exact frozen `ReleaseEvidence` instance.

The publisher must not render JSON from one snapshot, Markdown from another, or rebuild the terminal `GateRunResult` authority from mutable working state after publication.

`GateRunResult.evidence` must retain the exact terminal evidence object used for publication.

### 8.2 Publication success establishes terminal paths

The terminal publication path set is produced only after successful publication.

A partial path set is invalid. `evidence=None` with non-null publication paths is invalid. PASS requires coherent publication paths.

### 8.3 Publication failure always blocks PASS

If staging, write, verification, rename, or final publication fails, the terminal result must not be PASS even if the provisional evidence content would otherwise have been eligible.

The existing primary/cleanup/finalization failure precedence remains authoritative. No fallback or emergency path may reopen PASS.

### 8.4 Staging ownership semantics remain closed

Staging directories remain publication-class resources:

- acquire while physically live;
- release after verified disposal or successful atomic rename as defined by the existing implementation;
- retain ownership if cleanup genuinely fails and residue survives.

Staging residue blocks PASS through cleanup/finalization truth but must not be misclassified as `execution_resources_retained=True`.

## 9. Required TDD regression matrix

Before production changes, write RED tests that reproduce the valid v13 review findings and the approved v14 invariants.

Required RED cases include at least:

### Terminal result and proof validation

- PASS with `evidence=None` is rejected.
- PASS wrapping SCENARIO_FAILURE evidence is rejected.
- result/evidence overall disagreement is rejected.
- result/evidence exit-code disagreement is rejected.
- partial publication path set is rejected.
- PASS with unpublished or structurally incoherent evidence paths is rejected.
- final PASS validation rejects integer `event_uuid`.
- final PASS validation rejects raw UUID object.
- final PASS validation rejects `str` subclass UUID.
- `SignatureProof` rejects non-exact boolean or bool-spoofing input.
- `Authority` rejects scalar subclasses, opaque input, and noncanonical fields.

### Export algebra

- `_thaw()` rejects `str` subclass mapping key.
- `_thaw()` rejects custom/noncanonical mapping key.
- `_thaw()` rejects bytes, Path, UUID, Enum, and opaque object values.
- `_thaw()` still accepts the exact canonical frozen algebra.
- `to_dict()` remains fresh and detached.

### Resource ownership

- child explicitly parented to Q but located underneath P is not released by reconciling P.
- unparented resource under P is not inferred to be P's child.
- stale legacy parent tuple cannot override correct `parent_resource_id`.
- relocating a parent preserves parent ID and child traversal.
- relocating A onto B's active locator is rejected atomically.
- failed relocation leaves all locator indexes unchanged.
- `relocate()` cannot silently reparent or create graph cycles.
- rejected `set_parent()` cycle mutation is atomic.
- same-prefix sibling remains owned and retained.
- partial dump retains one stable resource ID from reservation through partial failure.
- a checked-in real `run_release_gate()` plus real `dual_deployment()` parent-relocation integration test passes.

The RED phase must fail on pre-v14 behavior for the intended causal reason, not because of fixture or environment setup errors.

## 10. Verification matrix

### 10.1 Focused v14 suite

Create and run:

`scripts/tests/test_release_gate_corrective_v14_*.py`

Acceptance requires zero failures and zero skips.

Clean-tree real lifecycle tests must actually execute; skip-based success is not accepted.

### 10.2 Corrective regression matrix v9-v14

Run the relevant corrective suites covering:

- v9 deep immutability, live ownership, run-ID validation;
- v10 emergency result, mapping detachment, resource classes, artifact ownership;
- v11 canonical values, structural ownership, artifact coverage;
- v12 export algebra, stable IDs, top-level terminal scalars;
- v13 evidence sealing, ID parent authority, terminal dataclasses, real lifecycle;
- v14 strict boundary closure.

Acceptance requires zero failures and zero skips for the focused corrective set.

If a previous test fixture treated PASS with missing evidence as valid, update the fixture to conform to the new invariant. Do not weaken production validation merely to preserve an invalid old fixture.

### 10.3 Exact-HEAD full verification

After implementation is committed, obtain:

- exact `git rev-parse HEAD`;
- clean `git status --short`.

On that exact committed HEAD run:

- `scripts/tests` suite;
- full pytest suite;
- `ruff check .`;
- Central Django check;
- Node Django check;
- Central migration drift check;
- Node migration drift check.

If code changes after the full-suite run, rerun the full suite on the new exact HEAD.

New warnings require adjudication. Previously known warnings may only be accepted when technically demonstrated to be non-blocking.

## 11. Literal release-gate acceptance

### 11.1 Development seven-scenario gate on macOS

Use the documented Python-level development/test mechanism with `check_platform=False`; do not add or invent a CLI bypass.

Expected result:

- overall = PASS
- exit_code = 0
- stage = DONE
- authority.authoritative = false
- authority.reason = development_only_platform
- execution_resources_retained = false
- primary_failure = null
- cleanup_failure = null
- finalization_failure = null

Exactly these scenarios must PASS:

- happy_path
- n1_tampered_replacement
- n2_revoked_credential
- n3_ack_lost_real_proxy
- n4_central_unavailable
- n5_node_restart
- backup_restore_smoke

### 11.2 Shipped macOS CLI refusal

The official CLI on macOS must continue to return PRECHECK_FAILURE with shell exit code 2.

No macOS bypass CLI flag is allowed in v14.

### 11.3 Controlled scenario failure

A controlled assertion failure through the real runner must produce:

- SCENARIO_FAILURE
- exit code 4
- stage DONE
- evidence published
- execution_resources_retained = false
- no cleanup/finalization failure unless a genuine cleanup/finalization failure occurs.

## 12. Adversarial acceptance cases

The final v14 suite must explicitly prove these formerly exploitable states are impossible or fail closed:

- PASS with no evidence;
- PASS with failure evidence;
- noncanonical proof UUID accepted by final validator;
- explicit parent Q overridden by parent P solely because of locator containment;
- locator collision during relocation mutating the ledger partially;
- relocation silently changing structural parentage;
- noncanonical export key passing through thaw.

The next independent reviewer should not need to invent out-of-repo probes to demonstrate these same failures.

## 13. Final residue audit

After literal acceptance runs, verify zero residual release-gate execution resources:

- gate databases;
- restore databases;
- connections to those databases;
- gate/Gunicorn processes;
- runtime workspaces;
- temporary `.llpkg` files;
- temporary dump files;
- raw runtime logs;
- private signing keys;
- unexpected release-gate temporary roots.

Intended sanitized release evidence in the artifact directory may remain.

Final `git status --short` must be empty.

## 14. Allowed production areas

Corrective v14 may touch these areas when necessary:

- `scripts/release_gate/evidence.py`
- `scripts/release_gate/environment.py`
- `scripts/release_gate/gate_runner.py`
- `scripts/release_gate/e2e_support.py`
- `scripts/release_gate/scenarios.py` only for required lifecycle call-site migration
- release-gate corrective tests

No other production area should change without explicit architectural approval.

## 15. Closure criteria

Corrective v14 is implementation-complete only when all of the following are true on one exact committed HEAD:

1. v14 RED tests were created before corresponding production fixes and then turned green.
2. Focused v14 suite has zero failures and zero skips.
3. Focused corrective v9-v14 regression set has zero failures and zero skips.
4. `scripts/tests` passes.
5. full pytest passes.
6. Ruff passes.
7. Central and Node Django checks pass.
8. Central and Node migration drift checks pass.
9. Fresh seven-scenario development release gate passes with non-authoritative macOS semantics.
10. Shipped macOS CLI refuses with PRECHECK_FAILURE / exit 2.
11. Controlled scenario failure returns SCENARIO_FAILURE / exit 4 with coherent evidence.
12. Final residue audit is clean.
13. Final working tree is clean and HEAD is unchanged from the verified candidate.
14. Independent Codex review classifies the result A with no valid Critical or Important findings.

Task 15 Task 11 remains OPEN until the coordinator adjudicates the independent review and explicitly closes it.

Task 15 Task 12 remains STOP until Task 15 Task 11 is formally closed.

## 16. Review mapping

Corrective v14 closes the valid v13 review findings as follows:

- Critical: contradictory `GateRunResult` terminal authority -> Sections 5.1-5.2 and 9.
- Critical: final PASS UUID coercion -> Section 5.3 and 9.
- Important: incomplete `SignatureProof` / `Authority` sealing -> Section 5.4 and 9.
- Important: containment recovery overriding explicit parent IDs -> Sections 6.1-6.3 and 9.
- Important: unsafe `relocate()` graph/locator behavior -> Sections 6.4-6.6 and 9.
- Important: export key validation gap -> Sections 7.1-7.4 and 9.
- Minor: temporary test-directory hygiene -> permitted only as a local test cleanup.
- Minor: missing durable real parent-relocation coverage -> Section 9 requires checked-in integration coverage.

No other architecture change is authorized by this corrective.
