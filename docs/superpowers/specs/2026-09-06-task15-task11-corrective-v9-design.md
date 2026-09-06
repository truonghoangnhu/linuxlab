# Task 15 Task 11 Corrective v9 — Deeply Immutable Terminal Snapshot, Complete Active Ownership, and Canonical Run-ID Validation

**Status:** Design approved; written-spec review pending
**Date:** 2026-09-06
**Base HEAD:** `0699158aad9d4067813b733cbbd49f69307359e5`
**Scope:** Task 15 Task 11 corrective only
**Task 12:** STOP
**T15-D28:** NO

**Intended repository path:**
`docs/superpowers/specs/2026-09-06-task15-task11-corrective-v9-design.md`

## 1. Purpose

This spec supersedes Task 11 Corrective v8.

The v8 independent review closed the previous restore-lifecycle and persisted/returned-equality findings, but identified three remaining Important blockers:

1. terminal evidence is still mutable and aliases nested input dictionaries;
2. `ResourceLedger` is active for restore databases but still misses ordinary Central/Node/workspace/outer/staging resources during their live ownership window;
3. canonical run-ID validation remains incomplete at both builder and final-validator boundaries.

Corrective v9 closes exactly these three gaps while preserving all v7/v8 CLOSED security and lifecycle boundaries.

Core invariants:

> All decision-bearing terminal evidence is deeply immutable and detached from mutable inputs.

> Every run-owned resource is registered in the ResourceLedger immediately after successful acquisition and released only after verified disposal.

> Builder and final validator use the same canonical run-ID grammar, while still validating independently at both boundaries.

Tasks 7–10 remain CLOSED.

## 2. Non-goals

Corrective v9 does not start Task 12–15, create T15-D28, change production Django models, provider architecture, `.llpkg` format/signing semantics, Task 14, or T15-D27. It does not add CI, deployment templates, new release features, or release tag/push behavior.

If implementation requires any of the above, STOP.

## 3. Preserve v8 CLOSED boundaries

These behaviors are baseline and must not regress:

- literal `RunState`;
- literal `ResourceLedger`;
- `Decision.UNDECIDED` initial state;
- real PRECHECK/STARTUP/SCENARIO/CLEANUP/FINALIZE/DONE transitions;
- prebuilt emergency terminal results;
- shipped CLI fixed-literal containment;
- no traceback/sentinel leak;
- scenario primary exit preserved through terminal collapse;
- no PASS without successful evidence publication;
- `evidence_dir=None` cannot PASS;
- publication circularity fail-closed;
- invalid timestamp offset `+00:99` rejected;
- durable resend `Inbox 1→2` rejects PASS;
- restore DB acquisition immediate;
- restore cleanup under CLEANUP;
- truthful restore cleanup status/retention;
- workspace recovery reconciles final retention;
- published failure evidence matches returned terminal truth;
- bundle-retention truth matches publication outcome.

## 4. Deeply immutable terminal snapshot

The canonical decision-bearing terminal representation must be deeply immutable.

Conceptually:

```python
@dataclass(frozen=True)
class TerminalEvidenceSnapshot:
    run: FrozenRunMetadata
    authority: FrozenAuthority
    environment: FrozenEnvironment
    profiles: FrozenProfiles
    validation: FrozenValidation
    scenarios: tuple[FrozenScenarioEvidence, ...]
    proofs: FrozenProofSet
    retention: FrozenRetention
    primary_failure: FrozenFailureRecord | None
    cleanup_failure: FrozenFailureRecord | None
    finalization_failure: FrozenFailureRecord | None
    cleanup_status: str
    overall: str
    exit_code: int
```

Exact names may differ.

No decision-bearing field may remain mutable after snapshot construction.

## 5. No mutable containers in terminal truth

Decision-bearing terminal data must not contain mutable `dict`, `list`, `set`, mutable dataclass, or mutable custom object directly or nested.

Use tuples, frozensets, frozen dataclasses, read-only value objects, or an immutable mapping representation.

A read-only wrapper over an aliased mutable dict is insufficient.

## 6. Detach snapshot from input objects

Snapshot construction must deep-copy/deep-freeze all mutable source values.

Example:

```python
source = {"passed": True}
snapshot = build_snapshot(source)
source["passed"] = False
```

Required: snapshot remains `passed=True`.

No decision-bearing mutable references may be shared with scenario results, proof dicts, validation dicts, retention dicts, environment metadata, or caller-owned structures.

## 7. Snapshot mutation attempts must fail

After publication/return, top-level and nested mutation attempts must fail or be structurally impossible.

Examples:

```python
snapshot.retention = ...
snapshot.scenarios[0].assertions["x"] = False
snapshot.proofs.outbox["inbox_count"] = 2
```

The returned `GateRunResult` must not expose a mutable decision-bearing object that can diverge from persisted JSON/Markdown.

## 8. One canonical snapshot remains the source

Required:

```text
final RunState facts known
→ build deep immutable snapshot
→ render JSON from snapshot
→ render Markdown from snapshot
→ publish
→ return same snapshot
```

No post-publication rebuild or decision-bearing mutation.

## 9. Renderer conversion

Renderers may convert the immutable snapshot into fresh plain data:

```text
immutable snapshot
→ to_plain_data() fresh detached dict/list
→ JSON / Markdown
```

Those plain structures are disposable and non-authoritative.

Mutating them must not mutate the snapshot.

## 10. Persisted/returned permanence

After successful publication, JSON, Markdown, and returned snapshot must remain semantically equal even if original inputs are mutated, returned mutation is attempted, or renderer-local data is changed.

## 11. Active ResourceLedger covers all gate-owned resources

Audit and wire active acquisition/release for at least:

- Central ephemeral DB;
- Node ephemeral DB;
- restore Central DB;
- restore Node DB;
- runtime workspace;
- outer run root;
- evidence staging directory;
- temporary llpkg/package artifact;
- backup/restore dump files;
- owned raw runtime logs or other temporary logs.

If a resource is not gate-owned, document why it is excluded.

## 12. Acquisition invariant

For every gate-owned resource:

```text
physical acquisition succeeds
→ ledger.acquire(resource)
→ caller regains control
```

There must be no failure window between successful creation and registration.

Forbidden:

```text
create
return
later infer from cleanup failure
```

Required:

```text
create
ledger.acquire
return
```

## 13. Ownership at acquisition boundary

Prefer registration inside or immediately adjacent to factories such as database/workspace/root/staging creation helpers, or pass the shared ledger into existing factories.

The invariant matters more than helper names.

## 14. Central/Node DB ownership

During live scenario execution, when Central or Node ephemeral DB physically exists, the shared ledger must already contain its ResourceRef.

No retrospective reconstruction from cleanup action names.

## 15. Workspace ownership

When runtime workspace exists, ledger contains it before scenario execution.

If inner cleanup fails but outer cleanup later succeeds, verified removal releases the workspace from the ledger.

## 16. Outer-root ownership

When outer run root is created, ledger contains it immediately.

After verified successful final root removal, release it.

## 17. Evidence staging ownership

When evidence staging directory is created, ledger contains it before publication/cleanup.

Atomic rename/transfer or cleanup must explicitly reconcile ownership.

## 18. Temporary package/dump/log ownership

For each release-gate-owned temporary artifact:

```text
creation succeeds → acquire
verified delete/transfer succeeds → release
```

If ownership belongs elsewhere, document that boundary and do not double-register.

## 19. ResourceKind classification

Distinguish at least conceptually:

```text
EXECUTION:
  CENTRAL_DB
  NODE_DB
  RESTORE_DB
  RUNTIME_WORKSPACE
  TEMP_LLPKG
  TEMP_DUMP
  RAW_RUNTIME_LOG

PUBLICATION:
  EVIDENCE_STAGING

INFRASTRUCTURE:
  OUTER_ROOT
```

Exact enum values may differ.

`execution_resources_retained` derives only from execution-class resources, while all kinds remain tracked for cleanup/audit.

## 20. Release invariant

A resource leaves the ledger only after verified disposal.

If cleanup fails, ledger entry remains and cleanup failure is recorded.

If later recovery succeeds, verify absence then release.

## 21. Live ownership tests

Permanent tests must inspect ownership during resource lifetime, not only at the end.

At minimum, before cleanup assert physical existence and ledger membership for Central DB, Node DB, workspace, outer root, staging, and restore DBs.

## 22. Partial-startup ownership

Inject failures immediately after one acquisition and before the next.

Examples:

```text
Central DB created; Node DB creation fails
workspace created; next startup operation fails
outer root created; later preflight/startup helper fails
```

Already-created resources must remain ledger-owned and directly visible to cleanup.

## 23. No ownership from cleanup-error names

Any action-name/string-matching logic may remain for diagnostics only. It must not be authoritative for ownership or retention.

## 24. Canonical run-ID validator

Introduce one pure canonical validator, conceptually:

```python
validate_canonical_run_id(value)
```

Use it at both:

```text
build_gate_environment()
validate_for_pass()
```

The final validator re-validates independently.

## 25. Canonical run-ID grammar

Use the intended existing project grammar. Do not invent a new arbitrary maximum length unless the current contract already specifies one.

At minimum reject:

```text
empty
non-str
str subclass where exact canonical str required
trailing newline
leading/trailing whitespace
embedded whitespace
path separators
path traversal forms
characters outside intended grammar
```

Examples:

```text
"rvalid
"
"r valid"
"../bad"
"path/bad"
""
True
1
None
```

Existing valid generated IDs must remain accepted.

## 26. Whole-string matching

Use full-string semantics such as `fullmatch`, `\Z`, or explicit character/range checks.

Do not use permissive `$`.

Do not `.strip()` malformed values into validity.

## 27. Builder boundary

`build_gate_environment()` must reject malformed canonical run IDs immediately.

Example:

```text
run_id="rvalid
" → builder rejection
```

Continue exact-string Git-SHA validation.

## 28. Final-validator boundary

An otherwise-valid candidate that bypasses the builder must still be rejected if run ID is malformed.

Example:

```text
candidate.run_id="../bad" → validate_for_pass rejects
```

## 29. Git-SHA strictness remains closed

Keep exact-string Git-SHA builder/final-validator behavior: canonical length/content, no subclass, no whitespace/newline.

## 30. Preserve published failure equality

V8's persisted JSON/Markdown/returned equality remains mandatory.

Deep immutability strengthens, not replaces, this invariant.

## 31. Preserve bundle retention

Published normal/failure evidence reports bundle retained=true everywhere; failed/absent publication reports false.

## 32. Preserve restore lifecycle

Keep immediate restore acquisition, partial acquisition tracking, CLEANUP-stage disposal, independent drop attempts, primary precedence, and residue=ledger truth.

## 33. Preserve publication-sealed PASS

PASS remains possible only if:

```text
no primary failure
no cleanup failure
no finalization failure
no retained execution resources
validate_for_pass succeeds
publication_outcome == PUBLISHED
```

## 34. Preserve emergency terminal behavior

If deep-freeze/snapshot construction fails catastrophically, use prebuilt emergency result with no traceback/sentinel and no evidence paths, preserving known primary exit.

## 35. Mandatory TDD RED tests

Use RED → GREEN.

1. Top-level snapshot mutation rejected.
2. Nested scenario/proof/validation/retention mutation rejected.
3. Original mutable input mutation does not affect snapshot.
4. Renderer plain-data mutation does not affect snapshot.
5. Published permanence after original-input mutation and returned mutation attempts.
6. Live Central DB physical=true/ledger=true.
7. Live Node DB physical=true/ledger=true.
8. Live workspace physical=true/ledger=true.
9. Live outer root physical=true/ledger=true.
10. Live staging physical=true/ledger=true.
11. Partial startup after Central creation still has Central ledger-owned.
12. Ownership remains correct even with cleanup action names that do not match legacy patterns.
13. Builder rejects `rvalid\n`, `r valid`, `../bad`, `path/bad`, empty, and str subclass.
14. Final validator rejects same values in bypassed candidates.
15. Existing canonical run IDs still pass.

## 36. Real integration acceptance

Successful dev override must observe physical=true and ledger=true during live ownership for Central DB, Node DB, workspace, outer root, restore DBs, and staging when present.

At completion:
- all execution resources released;
- deep immutable final snapshot;
- JSON/Markdown/returned truth permanently aligned;
- publication succeeds;
- stage DONE;
- Darwin authoritative=false;
- PASS/0.

Controlled failure must preserve exit 4, active ownership during lifetime, successful cleanup, immutable published failure snapshot, and no execution residue.

Partial-startup failure must prove all already-created resources are ledger-registered before failure and cleanup does not depend on error-name inference.

## 37. Regression requirements

Must remain green:

- Tasks 7–10;
- target safety;
- private-key protection;
- transactional keypair;
- partial setup cleanup;
- descriptor ownership;
- sensitive argv;
- ActionError;
- N1;
- N3;
- restart;
- authoring boundary;
- backup/restore;
- Task14;
- T15-D27;
- `.llpkg` format/semantics;
- v7 emergency containment;
- v7 publication-sealed PASS;
- v7 durable idempotence;
- v8 restore lifecycle;
- v8 workspace reconciliation;
- v8 bundle retention;
- v8 persisted/returned equality.

## 38. Expected scope

Likely Task 11-only files:

- `scripts/release_gate/gate_runner.py`
- `scripts/release_gate/e2e_support.py`
- `scripts/release_gate/environment.py`
- `scripts/release_gate/evidence.py`
- `scripts/release_gate/scenarios.py` only if acquisition plumbing requires it
- Task 11 tests

No production Django app changes expected.

## 39. Completion criteria

Corrective v9 is complete only when:

- terminal snapshot is deeply immutable;
- terminal snapshot is detached from all mutable source inputs;
- returned terminal truth cannot be mutated after publication;
- JSON/Markdown/returned truth remains permanently aligned;
- Central DB acquisition registered immediately;
- Node DB acquisition registered immediately;
- workspace acquisition registered immediately;
- outer-root acquisition registered immediately;
- staging acquisition registered immediately;
- all other gate-owned temp resources audited and actively registered or explicitly documented as non-owned;
- ledger entries removed only after verified disposal;
- ownership no longer depends on cleanup-error names;
- canonical run-ID grammar rejects newline/whitespace/path-like/invalid-character/non-str/subclass values as required;
- builder and final validator both apply the same canonical grammar independently;
- valid existing run IDs remain accepted;
- all v7/v8 CLOSED boundaries remain closed;
- full suite passes on exact final HEAD;
- Ruff/check/migration drift are clean;
- independent v9 review returns PASS.

## 40. STOP rule

After implementation:

**STOP.**

Request independent Task 11 Corrective v9 review.

Do not start Task 12 until the coordinator receives:

```text
A. TASK 15 TASK 11 CORRECTIVE PASS
```

## 41. Spec self-review

Completed before handoff:

- no Task 12/T15-D28 scope leakage;
- no production model/provider/Task14/T15-D27 change requested;
- no arbitrary run-ID maximum length invented;
- whole-string run-ID semantics explicit;
- immutable snapshot requires deep detachment, not only a frozen top-level dataclass;
- active ownership includes Central/Node/workspace/outer/staging resources during live ownership;
- execution/publication/infrastructure resource kinds distinguished;
- cleanup action names are diagnostics only, not ownership truth;
- builder and final validator remain independently strict;
- v7/v8 closed security boundaries explicitly preserved;
- STOP gate and independent review requirement explicit.

No unresolved design placeholder remains.
