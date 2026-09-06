# Task 15 Task 11 Corrective v11 — Canonical Terminal Values and Structural Resource Ownership

**Status:** Design approved; written-spec review pending
**Date:** 2026-09-06
**Base HEAD:** `a6593b4f86afcb161ea1c7089e50291b846741ab`
**Scope:** Task 15 Task 11 corrective only
**Task 12:** STOP
**T15-D28:** NO

**Intended repository path:**
`docs/superpowers/specs/2026-09-06-task15-task11-corrective-v11-design.md`

---

## 1. Purpose

This spec supersedes Task 11 Corrective v10.

The v10 independent review confirmed that several earlier boundaries are now closed, including ordinary emergency-result poisoning, resource-class separation, canonical run-ID validation, restore lifecycle, durable idempotence, publication-sealed PASS, and normal persisted/returned equality. However, four blockers remain:

1. mapping keys can remain caller-owned/mutable and alter returned trust after publication;
2. prefix-based parent/child reconciliation can release a surviving same-prefix execution artifact and allow PASS with residue;
3. scalar subclasses can survive terminal snapshot/export boundaries and preserve mutable behavior;
4. some gate-owned artifacts are still registered too late or not at all, especially tampered packages, restart/restored logs, and partial dump outputs.

Corrective v11 closes these gaps without changing domain architecture.

The two core rules are:

> **Terminal evidence accepts only a closed canonical value algebra with exact builtin leaf types and canonical mapping keys.**

> **Resource ownership relationships are explicit and structural; path-string prefix inference is forbidden. Side-effecting outputs are reserved in the ledger before the producer starts.**

Tasks 7–10 remain CLOSED.

---

## 2. Non-goals

Corrective v11 does not:

- start Task 12–15;
- create T15-D28;
- change production Django models;
- change provider architecture;
- change `.llpkg` format or signing semantics;
- change Task 14;
- change T15-D27;
- add CI or deployment features;
- add automatic global orphan sweeping;
- tag, push, or merge a release.

If implementation requires any of the above, STOP.

---

## 3. Preserve v10 CLOSED boundaries

The following remain baseline and must not regress:

- frozen top-level `GateRunResult`;
- cached emergency results protected against ordinary mutation;
- publication/no-publication PASS rules;
- CLI no traceback/sentinel;
- publication circularity fail-closed;
- canonical run-ID grammar;
- strict Git SHA behavior;
- `+00:99` timestamp rejection;
- durable resend `Inbox 1–2` rejection under optimized execution;
- PRECHECK/STARTUP/SCENARIO/CLEANUP/FINALIZE/DONE transitions;
- restore DB active acquisition and CLEANUP-stage disposal;
- workspace recovery reconciliation;
- bundle-retention truth;
- published scenario/finalization failure equality;
- resource classes EXECUTION / PUBLICATION / INFRASTRUCTURE.

---

## 4. Canonical terminal value algebra

All decision-bearing terminal evidence must be representable only by a closed set of approved values.

Conceptually:

```text
TerminalValue :=
    exact str
  | exact int
  | exact float
  | exact bool
  | None
  | tuple[TerminalValue, ...]
  | FrozenMapping[exact str, TerminalValue]
  | approved frozen terminal dataclass
```

The exact implementation may vary, but the semantic contract is strict.

---

## 5. Exact leaf types only

Do not use permissive `isinstance(value, str/int/bool/float)` for terminal leaves.

Use exact builtin type checks or equivalent canonicalization rules:

```python
type(value) is str
type(value) is int
type(value) is bool
type(value) is float
```

Custom subclasses carrying mutable attributes or overridden behavior are not accepted as canonical terminal leaves.

Reject unsupported subclasses instead of retaining or normalizing arbitrary object behavior.

---

## 6. Canonical mapping keys

Every terminal mapping key must be an exact canonical `str` unless a schema explicitly defines a different immutable typed field.

Default rule:

```text
mapping key
→ type(key) is str
→ accepted

otherwise
→ reject terminal snapshot construction
```

Do not accept:

- `str` subclasses;
- custom hashable objects;
- mutable key objects;
- caller-controlled key objects with mutable equality/hash behavior.

Do not normalize custom keys with `str(key)`.

---

## 7. Deep freeze must detach keys and values

For any `Mapping` input:

```text
iterate source mapping
→ validate each key
→ recursively freeze each value
→ build a NEW canonical immutable mapping
```

Never retain input mapping identity, input key identity, or mutable nested value identity.

This applies to:

- `dict`;
- `MappingProxyType`;
- custom `collections.abc.Mapping` implementations.

---

## 8. Mapping-key alias attack must fail

Required behavior:

```python
backing = {custom_mutable_key: False}
proxy = MappingProxyType(backing)
snapshot = build_snapshot(proxy)
```

If `custom_mutable_key` is not an exact canonical string:

```text
snapshot construction rejects it
```

There must be no path where mutating a caller-owned key after publication changes returned authority or lookup behavior.

---

## 9. Scalar-subclass attack must fail

Example:

```python
class MutableString(str):
    ...

value = MutableString("failure")
```

Required:

```text
terminal snapshot construction rejects unsupported subclass
```

Do not retain it inside:

- failure message;
- scenario assertion;
- proof field;
- metadata;
- renderer export.

---

## 10. to_dict/export must produce exact builtin leaves

Any `to_dict()` / render-export path must produce a fresh detached plain graph containing only exact builtin primitives and containers.

Required output forms:

```text
dict[str, ...]
list[...] / equivalent renderer-friendly plain arrays
exact str/int/float/bool/None leaves
```

No custom scalar subclass, custom key, mapping proxy, or arbitrary custom object may pass through.

Mutating export data must not affect the immutable terminal snapshot.

---

## 11. GateRunResult and emergency graph remain fully canonical

Top-level freezing from v10 remains.

Additionally, all nested terminal failure/evidence values reachable from ordinary and cached emergency `GateRunResult` objects must obey the canonical value algebra.

Prebuilt emergency globals are safe only if their entire object graph is both:

- structurally immutable;
- canonical-leaf safe.

---

## 12. Unsupported terminal values fail closed

If terminal snapshot construction encounters an unsupported value:

```text
→ record/fold into safe finalization failure if possible
→ otherwise use prebuilt emergency result
```

Do not silently coerce arbitrary objects.

Do not leak raw `repr`, traceback, or secret-bearing values.

---

## 13. Structural resource identity

`ResourceRef` must carry explicit structural identity rather than inferred path hierarchy.

Conceptually:

```python
ResourceRef(
    resource_id=...,
    kind=...,
    resource_class=...,
    locator=...,
    parent_resource_id=... | None,
)
```

Exact field names may differ.

The authoritative parent/child relation is `parent_resource_id`, not a path prefix.

---

## 14. Ban path-string prefix containment

The following or semantic equivalent is forbidden for ownership reconciliation:

```python
child_path.startswith(parent_path)
```

because:

```text
/tmp/run
/tmp/run-outside/file.dump
```

share a string prefix but are not parent/child.

If path containment is ever needed for verification, use true normalized path semantics (`Path.resolve()` / `relative_to()` or equivalent) and only as a secondary check, not as ownership source-of-truth.

---

## 15. Explicit parent/child relation at acquisition

When a child artifact is acquired, its parent is recorded explicitly.

Example:

```text
WORKSPACE
├── TEMP_LLPKG
├── TEMP_DUMP
└── RAW_LOG
```

The child remains individually owned throughout its lifetime.

Workspace ownership does not replace child ownership.

---

## 16. Parent cleanup reconciliation

After verified parent removal succeeds:

```text
for each registered child whose parent_resource_id == parent.resource_id:
    verify child's own locator
    if child absent:
        release child
    else:
        child remains owned

then release parent
```

Never release descendants solely because the parent path was removed or because their locator shares a prefix.

---

## 17. Parent cleanup failure

If parent removal fails:

```text
parent remains owned
```

Each child is independently verified:

- surviving child remains owned;
- already-absent child may be released after verified absence.

No blind descendant release.

---

## 18. Direct child deletion

If a child is deleted directly:

```text
verify child absent
→ release child only
```

Siblings and parent remain unaffected.

---

## 19. Resource relocation / move semantics

If gate code moves or renames an owned resource to a new physical locator, the ledger must be updated explicitly.

Conceptually:

```python
ledger.relocate(resource_id, new_locator, new_parent_resource_id=None)
```

or an equivalent safe operation.

Required invariant:

> A gate-owned resource must never move physically while the ledger continues to point only at its stale old locator.

---

## 20. Same-prefix sibling safety

Required probe:

```text
parent = /tmp/run
owned execution file = /tmp/run-outside/file.dump
```

The file is not a child unless explicitly registered with `parent_resource_id=parent.id`.

Removing `/tmp/run` must not release `/tmp/run-outside/file.dump`.

If the dump survives:

```text
execution_resources_retained=true
PASS impossible
```

---

## 21. Moved external child safety

If an originally child artifact is moved outside its parent:

```text
relocate(resource, external_path, parent=None or new_parent)
```

Then deleting the old parent must not release that artifact.

If external artifact survives:

```text
ledger retains it
execution_resources_retained=true
PASS impossible
```

---

## 22. Output-path reservation before side effects

For gate-owned file outputs produced by writers/subprocesses, reserve ownership before the side-effecting producer starts.

Conceptually:

```text
ledger.acquire_output_path(path, kind, class, parent)
run writer/subprocess
```

The ledger owns the path obligation even if the file does not yet exist.

This closes the partial-write window.

---

## 23. Producer succeeds

If producer succeeds and output exists:

```text
existing reservation already represents ownership
```

No later registration race exists.

---

## 24. Producer fails after partial write

If producer creates partial output and raises:

```text
partial file is already covered by ledger reservation
```

Cleanup must either:

- remove it and release the entry;
- or retain it and report execution residue.

---

## 25. Producer fails before creating output

If reserved output path never appears:

```text
cleanup verifies path absent
→ release reservation
```

This is not a retained resource.

---

## 26. Required artifact coverage

V11 must audit and cover every gate-created artifact identified by the v10 review:

### Packages
- primary `.llpkg`;
- tampered `.llpkg`.

### Logs
- initial Central log;
- initial Node log;
- `central-restarted.log`;
- `node-restarted.log`;
- `restored-central.log`;
- `restored-node.log`.

### Dumps
- Central dump;
- Node dump;
- partial Node dump / any partial dump created before producer failure.

Every gate-owned artifact must have an individual ledger entry or an explicit documented external-owner boundary.

---

## 27. Artifact ownership timing

For all required outputs:

```text
reserve ledger entry
→ producer begins
```

not:

```text
producer completes
→ then register
```

This is mandatory for commands/writers that can partially create files before failing.

---

## 28. Resource classes remain unchanged

Keep v10 classification:

```text
EXECUTION
PUBLICATION
INFRASTRUCTURE
```

Examples:

```text
Central/Node/restore DB        EXECUTION
workspace                      EXECUTION
llpkg/dump/log                 EXECUTION
staging                        PUBLICATION
outer root                     INFRASTRUCTURE
```

Do not redesign classification in v11.

---

## 29. Execution retention semantics

`execution_resources_retained` derives only from retained EXECUTION resources.

Therefore:

```text
outer-root-only residue
→ cleanup failure
→ non-PASS
→ execution_resources_retained=false
```

```text
staging-only residue
→ publication/finalization cleanup failure
→ non-PASS
→ execution_resources_retained=false
```

```text
surviving dump/log/llpkg/workspace/DB
→ execution_resources_retained=true
→ PASS impossible
```

---

## 30. No global orphan sweeper

Do not add a broad mechanism that scans and deletes stale databases/files from unrelated previous runs.

The pre-review orphan DBs observed in v10 were process residue from killed competing test runs and were removed before the clean independent review.

V11 only improves ownership of resources acquired by the current run.

---

## 31. Preserve publication and terminal boundaries

V11 must not reopen:

- no-publication PASS prohibition;
- successful publication requirement;
- bundle-retention truth;
- persisted/returned equality;
- emergency no-leak behavior;
- primary failure precedence.

Canonical-value changes strengthen the evidence boundary; they do not change terminal decision semantics.

---

## 32. TDD — mandatory RED tests

Use strict RED → GREEN.

### 32.1 Mutable/custom mapping key rejection

Create a custom/mutable hashable key or `str` subclass key inside:

- dict;
- MappingProxyType;
- custom Mapping.

Require snapshot construction rejection.

### 32.2 Published authority key attack

Attempt the exact v10 attack where caller mutates a key after publication.

Require returned authority and persisted evidence remain unchanged.

### 32.3 Scalar subclass rejection

Supply mutable `str`/int-like subclass in nested failure/proof/scenario fields.

Require rejection or safe canonicalization to exact builtin, with no alias/behavior retained.

### 32.4 Renderer exact-leaf export

`to_dict()` output must contain exact builtins only.

Mutating export must not affect snapshot/returned result.

### 32.5 Emergency graph canonicality

Verify cached emergency result graph contains no unsupported scalar subclass/custom mapping key and cannot be poisoned.

### 32.6 Same-prefix sibling

Register execution file `/tmp/run-outside/file.dump`; parent `/tmp/run` removed successfully.

Require file remains ledger-owned and prevents PASS.

### 32.7 Explicit child successful parent removal

Register real child under parent. Remove parent successfully. Verify child absent, then release child and parent.

### 32.8 Moved external child

Register child, move it outside parent, call `relocate()`, remove parent.

Require moved artifact remains owned and prevents PASS.

### 32.9 Direct child delete

Delete one child and verify only that child is released.

### 32.10 Parent cleanup failure

Parent removal fails; surviving children remain owned.

### 32.11 Tampered package live ownership

Observe tampered `.llpkg` while live and require individual ledger ownership.

### 32.12 Restart/restored log ownership

Observe all restart/restored logs while live and require individual ledger ownership.

### 32.13 Dump reservation before producer

Before Central and Node dump producers start, reservation exists.

### 32.14 Partial dump failure

Force Node dump producer to partially create output then raise.

Require artifact already ledger-owned before cleanup.

### 32.15 Producer fails before create

Reserved path absent after producer failure. Cleanup verifies absence and releases reservation.

---

## 33. Real integration acceptance

### Successful dev override

During the real seven-scenario run, verify live individual ownership for:

- primary llpkg;
- tampered llpkg;
- Central/Node initial logs;
- restarted Central/Node logs;
- restored Central/Node logs;
- Central dump;
- Node dump.

At completion:

- no retained execution resource;
- structural ledger clean;
- immutable canonical terminal graph;
- PASS/0 non-authoritative on Darwin dev;
- stage DONE.

### Same-prefix adversarial run

Use PASS-worthy scenario observations but leave a real registered execution artifact at a same-prefix sibling path.

Required:

```text
ledger retains artifact
execution_resources_retained=true
PASS impossible
```

### Partial dump adversarial run

Create partial dump then fail producer and force/inspect cleanup.

Required:

- artifact individually owned before cleanup;
- successful cleanup releases it;
- failed cleanup retains it and blocks PASS.

---

## 34. Regression requirements

Must remain green:

- Tasks 7–10;
- target safety;
- private-key protections;
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
- v8 published-failure equality;
- v9 canonical run-ID;
- v10 resource-class semantics;
- v10 ordinary emergency-poison regression.

---

## 35. Expected scope

Likely Task 11-only files:

- `scripts/release_gate/evidence.py`
- `scripts/release_gate/gate_runner.py`
- `scripts/release_gate/e2e_support.py`
- `scripts/release_gate/scenarios.py`
- Task 11 tests

No production Django app changes expected.

---

## 36. Completion criteria

Corrective v11 is complete only when:

- terminal mapping keys are canonical exact strings or otherwise explicitly typed;
- unsupported/custom mapping keys are rejected;
- scalar subclasses cannot survive terminal snapshot/export boundaries;
- `to_dict()` exports exact builtin leaves only;
- ordinary and emergency terminal graphs remain deeply immutable and detached;
- no path-prefix ownership reconciliation remains;
- parent/child ownership is explicit;
- child release requires verified child absence;
- moved resources update ledger locator/parent explicitly;
- same-prefix siblings cannot be accidentally released;
- gate-owned output paths are reserved before producer side effects;
- primary and tampered llpkg artifacts are individually tracked;
- all initial/restart/restored logs are individually tracked;
- Central/Node/partial dump outputs are individually tracked before producer completion;
- execution-retention truth remains class-correct;
- PASS cannot occur with any retained execution resource;
- all v7/v8/v9/v10 CLOSED boundaries remain closed;
- scripts suite and full pytest pass on exact final HEAD;
- Ruff/check/migration drift clean;
- independent v11 review returns PASS.

---

## 37. STOP rule

After implementation:

**STOP.**

Request independent Task 11 Corrective v11 review.

Do not start Task 12 until the coordinator receives:

```text
A. TASK 15 TASK 11 CORRECTIVE PASS
```

---

## 38. Spec self-review

Completed before handoff:

- no Task 12/T15-D28 scope leakage;
- no production model/provider/Task14/T15-D27 change requested;
- no global orphan sweeper added;
- mapping keys are explicitly part of the canonical terminal contract;
- scalar subclasses are explicitly excluded from safe terminal leaves;
- renderer exports are required to use exact builtins;
- path-prefix containment is explicitly forbidden;
- parent/child ownership is explicit, structural, and relocation-aware;
- parent cleanup verifies each child before release;
- output paths are reserved before side-effecting producers to cover partial writes;
- all artifact creation sites identified by the v10 review are explicitly listed;
- v10 resource-class semantics are preserved;
- STOP gate and independent review requirement are explicit.

No unresolved design placeholder remains.
