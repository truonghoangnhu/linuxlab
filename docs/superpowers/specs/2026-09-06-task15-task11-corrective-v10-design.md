# Task 15 Task 11 Corrective v10 — Immutable Terminal Result Graph and Typed Resource Ownership Tree

**Status:** Design approved; written-spec review pending
**Date:** 2026-09-06
**Base HEAD:** `6d8712eea5953869c2d98c0c871ebf1409f6d62f`
**Scope:** Task 15 Task 11 corrective only
**Task 12:** STOP
**T15-D28:** NO

**Intended repository path:**
`docs/superpowers/specs/2026-09-06-task15-task11-corrective-v10-design.md`

## 1. Purpose

This spec supersedes Task 11 Corrective v9.

The v9 independent review confirmed that run-ID grammar, live ownership of the principal resources, restore lifecycle, publication-sealed PASS, durable resend protection, stage transitions, and ordinary published-evidence equality are closed. Four blockers remain:

1. cached emergency `GateRunResult` objects are mutable and can be poisoned so a later failing run returns `PASS/0` without publication;
2. supplied read-only mapping wrappers such as `MappingProxyType` can preserve aliases to caller-owned mutable data, allowing returned authority/trust fields to diverge from persisted evidence;
3. infrastructure/publication resources are incorrectly counted as retained execution resources;
4. gate-owned package, dump, and log artifacts are not individually represented in the ownership model.

Corrective v10 closes these gaps without changing domain architecture.

Core invariants:

> The complete terminal result graph, including emergency results, is deeply immutable and detached from caller-owned mutable state.

> ResourceLedger uses typed resource classes and explicit ownership relationships so execution retention, publication retention, and infrastructure retention cannot be conflated.

> Gate-owned child artifacts are individually owned even when their physical disposal is delegated to an enclosing workspace/root cleanup.

Tasks 7–10 remain CLOSED.

## 2. Non-goals

Corrective v10 does not start Task 12–15, create T15-D28, change production Django models, provider architecture, `.llpkg` format/signing semantics, Task14, or T15-D27. It does not add CI/deployment templates or new release behavior.

If implementation requires any of the above, STOP.

## 3. Preserve CLOSED v7/v8/v9 boundaries

Must remain closed:

- emergency terminal containment;
- CLI fixed-literal no-leak behavior;
- no PASS without successful publication;
- no PASS for `evidence_dir=None`;
- publication circularity fail-closed;
- `+00:99` timestamp rejection;
- durable resend `Inbox 1->2` rejection;
- PRECHECK/STARTUP/SCENARIO/CLEANUP/FINALIZE/DONE transitions;
- restore DB acquisition and CLEANUP-stage disposal;
- workspace recovery reconciliation;
- bundle-retention truth;
- ordinary persisted JSON/Markdown/returned equality;
- canonical run-ID grammar at builder and final validator;
- live principal-resource ownership for Central/Node DBs, workspace, outer root, staging, and restore DBs.

## 4. Immutable terminal result graph

The entire returned terminal graph must be immutable, not only `ReleaseEvidence`.

Conceptually:

```python
@dataclass(frozen=True)
class GateRunResult:
    overall: str
    exit_code: int
    evidence: ReleaseEvidence | None
    terminal_failure: FrozenFailureRecord | None
    json_path: str | None
    markdown_path: str | None
    stage: str
```

Exact field names may differ.

Requirements:

- `GateRunResult` is frozen/immutable;
- `ReleaseEvidence` remains deeply immutable;
- `terminal_failure` is immutable;
- nested mappings/collections are immutable and detached;
- evidence path values are immutable scalars;
- no caller can mutate decision-bearing state after return.

## 5. Emergency result cache safety

Prebuilt `_EMERGENCY_RESULTS` may remain global only if every object in the cached graph is immutable.

Required:

```text
cached emergency object
-> caller cannot mutate overall/exit/evidence/failure
-> later failing run observes original deterministic failure result
```

Forbidden:

```text
mutate cached emergency result to PASS/0
-> reused by subsequent run
```

A fresh copy per run is optional; immutability is mandatory.

## 6. Emergency result poison-resistance test

Permanent test:

1. obtain an emergency result;
2. attempt to mutate top-level fields;
3. attempt to mutate nested failure data;
4. execute a second failing run using the same emergency path;
5. require deterministic nonzero result and no publication.

The test must prove no cached global can be poisoned.

## 7. Deep detach of all Mapping inputs

`deep_freeze()` or equivalent must treat every `collections.abc.Mapping` as untrusted input.

Required:

```text
Mapping input
-> iterate keys/values
-> recursively detach/freeze each key/value
-> construct NEW immutable mapping
```

Do not preserve input identity for:

- `dict`;
- `MappingProxyType`;
- custom Mapping implementations.

A read-only wrapper is not sufficient if it still references caller-owned mutable backing data.

## 8. Supported immutable mapping representation

Use a genuinely detached immutable representation, e.g.:

- immutable/frozen mapping value object;
- tuple of frozen key/value pairs with deterministic lookup;
- equivalent internal representation.

Exact implementation is flexible.

Requirements:

- no mutable backing object remains reachable;
- nested values are recursively frozen;
- rendering can convert it to fresh plain data.

## 9. Unsupported mutable/custom values

If snapshot construction receives an unsupported mutable custom object that cannot be safely detached/frozen:

```text
-> reject snapshot construction
-> record safe finalization failure if possible
-> otherwise use prebuilt emergency result
```

Do not retain opaque mutable references.

## 10. Renderer detachment

`to_dict()` / render conversion must create a new deep mutable export every call.

Required:

```text
immutable snapshot
-> to_dict()
-> fresh dict/list tree
```

Mutating exported data must never mutate:

- the snapshot;
- GateRunResult;
- cached emergency data;
- future runs.

## 11. Published authority permanence

Permanent adversarial case:

```text
backing = {"authoritative": False}
proxy = MappingProxyType(backing)
snapshot = build_snapshot(proxy)
publish(snapshot)
backing["authoritative"] = True
```

Required:

- persisted JSON remains false;
- persisted Markdown remains false;
- returned snapshot remains false;
- no alias exists to backing data.

## 12. ResourceClass taxonomy

Introduce a closed resource class/type distinction, conceptually:

```text
EXECUTION
PUBLICATION
INFRASTRUCTURE
```

Resource kinds map to exactly one class.

Suggested mapping:

```text
CENTRAL_DB          -> EXECUTION
NODE_DB             -> EXECUTION
RESTORE_DB          -> EXECUTION
RUNTIME_WORKSPACE   -> EXECUTION
TEMP_LLPKG          -> EXECUTION
TEMP_DUMP           -> EXECUTION
RAW_RUNTIME_LOG     -> EXECUTION

EVIDENCE_STAGING    -> PUBLICATION

OUTER_ROOT          -> INFRASTRUCTURE
```

Exact enum names may differ.

## 13. Execution-retention derivation

`execution_resources_retained` is true only when retained resources include class `EXECUTION`.

Required:

```text
outer-root-only residue
-> execution_resources_retained = false
-> cleanup failure still prevents PASS

staging-only residue
-> execution_resources_retained = false
-> cleanup/publication failure still prevents PASS
```

This separates semantic retention reporting from overall release-gate success.

## 14. All-resource cleanup truth

Even non-execution resources remain fully tracked.

A retained publication/infrastructure resource must still:

- remain in the ledger;
- appear in safe cleanup/audit evidence where applicable;
- create cleanup/finalization failure as appropriate;
- prevent PASS if cleanup/publication invariants are not satisfied.

Only the `execution_resources_retained` field is filtered by resource class.

## 15. Gate-owned artifact-level ownership

The following gate-owned artifacts require individual ledger entries while physically present:

- generated `.llpkg` temporary package;
- backup/restore dump files;
- owned runtime log files.

When each artifact is successfully created:

```text
physical file exists
-> ledger.acquire(child resource)
-> caller regains control
```

## 16. Ownership tree

Represent containment/ownership relationships, conceptually:

```text
OUTER_ROOT
├── RUNTIME_WORKSPACE
│   ├── TEMP_LLPKG
│   ├── TEMP_DUMP
│   └── RAW_RUNTIME_LOG
└── EVIDENCE_STAGING
```

Exact data structure may vary.

Required behavior:

- child resources are individually visible during lifetime;
- parent cleanup may dispose children physically;
- verified parent removal may release all contained child entries;
- failed parent removal leaves surviving children owned.

## 17. Individual child disposal

If a child artifact is removed independently:

```text
verify child absent
-> ledger.release(child)
```

If parent workspace/root removal later removes remaining children:

```text
verify parent absent
-> release contained descendants
-> release parent
```

Do not require redundant per-file delete operations merely for ledger bookkeeping.

## 18. Workspace deletion semantics

Successful verified workspace removal:

```text
workspace absent
-> all workspace-contained gate-owned artifact refs are considered disposed
-> release TEMP_LLPKG/TEMP_DUMP/RAW_RUNTIME_LOG descendants
-> release workspace
```

Failed workspace removal:

```text
workspace remains
-> surviving descendants remain ledger-owned
```

If some descendants were already independently deleted, only surviving ownership remains.

## 19. Outer-root deletion semantics

Successful verified outer-root removal may reconcile remaining contained transient resources.

However, it must not erase a child entry if the child was moved/transferred outside the root and remains owned elsewhere.

Ownership transfer, if any, must be explicit.

## 20. Evidence staging ownership transition

During publication:

```text
staging created
-> acquire EVIDENCE_STAGING
```

If atomic rename turns staging into final evidence:

```text
old staging path absent
-> release EVIDENCE_STAGING
-> final evidence bundle is not a transient execution resource
```

If publication fails and staging cleanup succeeds, release after verified absence.

If staging cleanup fails, staging remains ledger-owned as PUBLICATION.

## 21. No action-name ownership inference

V9's removal of `_CLEANUP_ACTION_TO_RESOURCE_KIND` and `_reconcile_scenario_resources()` remains permanent.

Action names/strings may be diagnostics only.

Ownership and retention derive exclusively from explicit ledger state and typed ownership relationships.

## 22. Run-ID grammar remains closed

No functional run-ID redesign in v10.

Regression only:

```text
rvalid\n  -> reject
r valid   -> reject
../bad    -> reject
path/bad  -> reject
valid ID  -> accept
```

Do not invent a new maximum length.

## 23. Preserve publication-sealed PASS

PASS remains possible only when:

```text
no primary failure
no cleanup failure
no finalization failure
no retained EXECUTION resources
validate_for_pass succeeds
publication_outcome == PUBLISHED
```

Additionally, retained PUBLICATION/INFRASTRUCTURE resources may block PASS through their cleanup/publication failure state.

No alternate PASS path.

## 24. Preserve emergency containment

If immutable graph construction fails catastrophically:

```text
prebuilt immutable emergency result
```

No dynamic exception text, traceback, sentinel leak, or evidence publication requirement.

Known primary exit precedence remains.

## 25. Mandatory TDD RED tests

Use strict RED -> GREEN.

### 25.1 Ordinary GateRunResult mutation
Attempt to mutate overall, exit, evidence reference, stage, terminal failure.

Require rejection.

### 25.2 Emergency GateRunResult mutation
Same mutation attempts on cached emergency result.

Require rejection.

### 25.3 Cached emergency poison regression
Attempt mutation, then run a second terminal failure.

Require deterministic nonzero and no publication.

### 25.4 MappingProxyType backing alias
Build snapshot from proxy backed by mutable dict, mutate backing after snapshot/publication.

Require snapshot and persisted evidence unchanged.

### 25.5 Nested proxy alias
MappingProxyType contains nested mutable dict/list.

Mutate nested backing.

Require snapshot unchanged.

### 25.6 Renderer export mutation
Mutate `to_dict()` deeply.

Require snapshot/result unchanged.

### 25.7 Outer-root-only residue
Require ledger-owned infrastructure residue, cleanup failure, execution retention=false, non-PASS.

### 25.8 Staging-only residue
Require publication residue, execution retention=false, non-PASS.

### 25.9 Live llpkg ownership
While `.llpkg` exists, individual ledger entry exists.

### 25.10 Live dump ownership
While dump exists, individual ledger entry exists.

### 25.11 Live log ownership
While owned runtime log exists, individual ledger entry exists.

### 25.12 Workspace successful cleanup
Workspace deletion removes descendants and releases corresponding entries.

### 25.13 Workspace cleanup failure
Surviving children remain owned.

### 25.14 Child independent cleanup
Individually removed artifact releases only itself.

## 26. Real integration acceptance

Successful dev override must demonstrate during execution:

- Central DB tracked;
- Node DB tracked;
- restore DBs tracked;
- workspace tracked;
- outer root tracked;
- staging tracked when present;
- temporary llpkg tracked while present;
- dump tracked while present;
- owned runtime log tracked while present.

At terminal success:

- no retained execution resources;
- no stale transient publication/infrastructure resources;
- immutable GateRunResult;
- immutable terminal evidence graph;
- JSON/Markdown/returned truth aligned;
- publication succeeded;
- PASS/0 only after publication.

## 27. Controlled failure acceptance

Real controlled scenario failure must:

- remain exit 4 when primary scenario failure exists;
- preserve cleanup/finalization secondary truth;
- dispose execution resources successfully when cleanup works;
- retain sanitized evidence only;
- expose immutable failure result.

## 28. Publication/infrastructure residue acceptance

Inject outer-root-only residue and staging-only residue separately.

Require:

```text
execution_resources_retained = false
cleanup/publication failure = true
PASS = false
```

This is a required semantic distinction.

## 29. Regression requirements

Must remain green:

- Tasks 7–10;
- target safety;
- private-key 0600-before-write;
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
- v8 workspace recovery;
- v8 bundle retention;
- v8 persisted/returned equality;
- v9 canonical run-ID validation;
- v9 live principal-resource ownership.

## 30. Expected scope

Likely Task 11-only files:

- `scripts/release_gate/evidence.py`
- `scripts/release_gate/gate_runner.py`
- `scripts/release_gate/e2e_support.py`
- `scripts/release_gate/scenarios.py`
- Task 11 tests

No production Django app changes expected.

## 31. Completion criteria

Corrective v10 is complete only when:

- `GateRunResult` is immutable;
- cached emergency result graph is immutable;
- no emergency result can be poisoned across runs;
- all Mapping inputs are recursively detached/frozen, including `MappingProxyType`;
- nested backing-object mutations cannot change returned/persisted truth;
- renderer exports are fully detached;
- resource classes distinguish EXECUTION/PUBLICATION/INFRASTRUCTURE;
- outer-root-only and staging-only residues do not set execution retention true;
- gate-owned llpkg/dump/log artifacts have individual live ownership entries;
- workspace/root cleanup correctly reconciles child ownership;
- no ownership inference from cleanup action names returns;
- all v7/v8/v9 CLOSED boundaries remain closed;
- full scripts suite and full pytest pass on exact final HEAD;
- Ruff, Central/Node checks and migration drift are clean;
- independent v10 review returns PASS.

## 32. STOP rule

After implementation:

**STOP.**

Request independent Task 11 Corrective v10 review.

Do not start Task 12 until the coordinator receives:

```text
A. TASK 15 TASK 11 CORRECTIVE PASS
```

## 33. Spec self-review

Completed before handoff:

- no Task 12/T15-D28 scope leakage;
- no production model/provider/Task14/T15-D27 change requested;
- emergency cache poisoning is explicitly covered;
- MappingProxyType/read-only-wrapper aliasing is explicitly covered;
- deep renderer detachment is explicit;
- resource classes prevent execution/publication/infrastructure conflation;
- llpkg/dump/log individual ownership is explicit;
- parent-child cleanup semantics avoid redundant deletes while retaining truthful ownership;
- action-name ownership inference remains prohibited;
- run-ID grammar remains unchanged and regression-only;
- publication-sealed PASS remains intact;
- STOP gate and independent review requirement are explicit.

No unresolved design placeholder remains.
