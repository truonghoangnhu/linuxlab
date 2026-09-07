# Task 15 Task 11 Corrective v13 — Sealed Terminal Object Graph and ID-Native Ownership Traversal

**Status:** Design approved; written-spec review pending
**Date:** 2026-09-07
**Base HEAD:** `0e99721e52a16322e3e99192ceceb45a3742dcb3`
**Scope:** Task 15 Task 11 corrective only
**Task 12:** STOP
**T15-D28:** NO

**Intended repository path:**
`docs/superpowers/specs/2026-09-07-task15-task11-corrective-v13-design.md`

---

## 1. Purpose

This spec supersedes Corrective v12.

The v12 independent review confirmed that the following are CLOSED and must remain closed: `ReleaseEvidence` scalar sealing, raw `bytes` rejection, fail-closed freeze/thaw/export, stable `resource_id`, same-prefix sibling handling, artifact ownership/reservation, partial-dump same-ID, resource-class filtering, emergency immutability, canonical run-ID, restore lifecycle, publication-sealed PASS, durable resend rejection, and the literal seven-scenario dev gate.

Three blockers remain:

1. `GateRunResult.evidence` can still retain arbitrary mutable/custom evidence by identity.
2. Supporting terminal dataclasses and path-bearing result fields can still retain unsupported scalar/custom identity.
3. `parent_resource_id` exists but reconciliation still traverses legacy locator tuples, so stable ID parentage is not authoritative.

Corrective v13 closes exactly these three gaps.

Core invariants:

> Every object participating in terminal decision truth is constructor-sealed before it becomes authoritative.

> `GateRunResult.evidence` is exactly `ReleaseEvidence | None`.

> Structural ownership traversal uses `parent_resource_id` as the sole authoritative parent relation.

> Parent relocation cannot break child ownership because structure is tied to stable IDs, not locators.

---

## 2. Non-goals

V13 does not start Task 12–15, create T15-D28, alter production Django/Wagtail models or migrations, alter Task14/T15-D27/provider architecture, change `.llpkg` format/signing, redesign publication, redesign canonical run-ID, redesign restore lifecycle, add a global orphan sweeper, or add a new dependency.

If implementation requires any of these, STOP.

---

## 3. Preserve CLOSED boundaries

The implementation must preserve:

- `ReleaseEvidence` exact scalar sealing;
- exact-string mapping keys;
- MappingProxy/custom Mapping detachment;
- raw `bytes` rejection;
- fail-closed freeze/thaw/export;
- exact JSON-safe evidence export;
- frozen emergency result graph;
- no-publication PASS prohibition;
- CLI no-leak;
- publication circularity fail-closed;
- `+00:99` rejection;
- durable resend Inbox `1→2` rejection;
- PRECHECK→STARTUP→SCENARIO→CLEANUP→FINALIZE→DONE;
- stable resource IDs;
- same-prefix sibling protection;
- artifact reservation and partial-dump same-ID;
- restore lifecycle and workspace recovery;
- resource-class filtering;
- bundle-retention truth;
- published failure equality;
- canonical run-ID.

---

# Part A — Sealed Terminal Object Graph

## 4. `GateRunResult.evidence` is a closed union

`GateRunResult.evidence` accepts only:

```text
exact ReleaseEvidence
or
None
```

Normative check:

```python
if evidence is not None and type(evidence) is not ReleaseEvidence:
    raise TypeError(...)
```

Reject dicts, mappings, mapping proxies, `ReleaseEvidence` subclasses, custom evidence objects, wrappers, and mutable proxies.

A frozen outer result is insufficient if it retains a mutable evidence reference.

## 5. Result-shape consistency

`GateRunResult` validates coherent evidence/path state. Invalid combinations include:

- arbitrary evidence object with published paths;
- PASS result lacking required published evidence;
- emergency/no-publication result claiming published evidence paths;
- unsupported/custom path representations.

Existing valid result shapes remain unchanged.

## 6. Terminal paths use canonical strings

Terminal result truth should not retain `Path`, `PosixPath`, `WindowsPath`, custom `Path` subclasses, or arbitrary `os.PathLike` identity.

Preferred representation:

```text
json_path: exact str | None
markdown_path: exact str | None
other terminal paths: exact str | None
```

Filesystem paths are converted to canonical strings before constructing terminal result truth.

## 7. All supporting terminal dataclasses are constructor-sealed

Audit and seal every frozen/value dataclass reachable from `ReleaseEvidence` or `GateRunResult`, including at least:

- `OutboxInboxProof`;
- `ScenarioResult`;
- `PassDecision`;
- failure/terminal failure records;
- authority/validation/retention proof records;
- any other frozen terminal record discovered in code.

No supporting terminal dataclass may depend on a later snapshot to reject malformed values.

## 8. Exact scalar policy

Schema scalar fields accept exact expected types only:

```text
string → type(value) is str
integer → type(value) is int
float → exact schema-defined numeric type
boolean → type(value) is bool
optional → None or exact expected type
```

Reject scalar subclasses and arbitrary conversion objects.

Do not canonicalize with `str(custom)`, `int(custom)`, or similar user-defined conversion at the terminal boundary.

## 9. UUID representation

Terminal proof/state must not retain raw UUID objects.

Convert at a trusted pre-terminal boundary:

```text
UUID/domain value → canonical UUID string → terminal dataclass
```

Terminal dataclass accepts exact canonical string only.

## 10. Enum/custom-object representation

Enums and custom semantic objects must be converted to their existing canonical string/int representation before terminal construction.

No Enum/custom identity survives in terminal truth.

## 11. `PassDecision` is sealed

Every field is validated at construction. No unsupported/custom value may enter PASS decision state by identity.

## 12. `ScenarioResult` is sealed

At minimum:

```text
name → exact str
status → exact existing canonical representation
duration_ms → exact schema-defined numeric type
detail → exact str or None
```

Reject scalar subclasses/custom objects.

## 13. `OutboxInboxProof` is sealed

At minimum:

```text
event UUID → exact canonical str
counts → exact int
idempotence flags → exact bool
other proof scalars → exact schema types
```

Reject raw UUID objects and scalar subclasses.

## 14. Failure records remain sealed

Preserve v12 failure-record behavior: frozen, exact canonical scalars, no custom mapping/leaf identity, no mutable aliasing.

## 15. Export remains fail closed

No freeze/thaw/export path may reintroduce unknown-value passthrough.

Supporting dataclasses must already be safe before evidence assembly; export is defense-in-depth, not the first validation boundary.

## 16. Terminal graph completeness

Required graph property:

```text
GateRunResult
├── exact builtin scalar fields
├── canonical path strings / None
├── exact ReleaseEvidence / None
│   └── sealed terminal graph only
└── sealed terminal failure / None
```

No reachable node may retain caller-owned mutable/custom identity.

---

# Part B — ID-Native Structural Ownership

## 17. `parent_resource_id` is authoritative

Structural ownership uses `child.parent_resource_id` as the sole authoritative relation.

Legacy `parent_kind` / `parent_safe_id` may remain temporarily for compatibility, diagnostics, or recovery, but cannot decide:

- child traversal;
- release behavior;
- retention behavior;
- PASS eligibility.

## 18. `children_of(parent_id)` traverses by ID

Normative semantics:

```python
children = [
    record
    for record in active_records
    if record.parent_resource_id == parent_id
]
```

An index is allowed. Locator matching is not.

## 19. Parent relocation invariant

Given parent `P1` and child `C.parent_resource_id == P1`, relocating the parent's locator must not change the relationship:

```text
relocate(P1, new_locator)
→ children_of(P1) still contains C
```

## 20. Parent cleanup after relocation

Mandatory lifecycle:

```text
acquire parent P
acquire child C under P
relocate P
remove relocated parent physically
reconcile_verified_parent_removal(P)
```

Required:

```text
children found by parent ID
child's own locator verified
absent child released
parent released
no false execution retention
```

## 21. `set_parent()` semantics

`set_parent(child_id, parent_id)` must:

- validate child exists;
- validate parent exists;
- reject self-parenting;
- reject cycles if the API can create them;
- update `child.parent_resource_id`;
- make `children_of(parent_id)` observe the child immediately.

## 22. Cycle safety

If arbitrary nesting is possible, prevent cycles with a bounded ancestor check. Do not build a generic graph subsystem.

## 23. Legacy tuple is mirror/recovery metadata only

If retained:

```text
parent_resource_id = authoritative
legacy tuple = mirror/recovery metadata
```

It may be recomputed when needed but never override the ID relation.

## 24. Deliberate disagreement test

Create a child where:

```text
parent_resource_id → P1
legacy tuple → stale P0 locator
```

Required:

```text
children_of(P1) contains child
stale tuple has no authority
cleanup follows P1
```

## 25. Real call-path migration

V12's scope narrowing is no longer sufficient.

Audit lifecycle-dependent call sites in:

- `scripts/release_gate/e2e_support.py`;
- `scripts/release_gate/scenarios.py`;
- `scripts/release_gate/gate_runner.py`.

Only operations whose correctness depends on stable structure need migration. Avoid unrelated churn.

Operations that must use stable IDs where relevant:

```text
acquire/reserve
set_parent
children_of
relocate
reconcile_verified_parent_removal
release
```

## 26. Real resource IDs remain stable

Preserve stable IDs for outer root, workspace, Central/Node DBs, restore DBs, staging, primary/tampered llpkg, initial/restart/restore logs, and Central/Node dumps.

## 27. Output reservations remain same-ID

Preserve:

```text
reserve output → resource_id R
producer runs
success/partial failure → same R
cleanup/relocate → R until verified release
```

## 28. Same-prefix sibling remains closed

No string-prefix ownership inference may return.

## 29. Child relocation outside parent remains closed

Regression:

```text
move child externally
relocate same child ID and clear/change parent
cleanup old parent
→ child remains owned
→ execution retention true
→ PASS impossible
```

## 30. Path-containment fallback is recovery-only

A genuine `Path.resolve()/relative_to()` fallback may remain only for legacy/unparented recovery cases.

It must never create authoritative parentage, override `parent_resource_id`, release surviving external resources, or influence PASS directly.

---

# Part C — TDD and Acceptance

## 31. RED tests — `GateRunResult.evidence`

Permanent tests:

```text
GateRunResult(evidence=dict) → reject
GateRunResult(evidence=MappingProxyType(...)) → reject
GateRunResult(evidence=ReleaseEvidence subclass) → reject
GateRunResult(evidence=exact ReleaseEvidence) → accept
GateRunResult(evidence=None) → accept only in valid result shapes
```

## 32. RED tests — terminal path fields

Test custom Path subclass, custom `os.PathLike`, bytes path, opaque object.

Required: terminal result rejects them or receives only a trusted pre-converted exact string.

## 33. Supporting dataclass field matrix

For every field of `OutboxInboxProof`, `ScenarioResult`, `PassDecision`, failure records, and other reachable terminal dataclasses, inject relevant unsupported values:

```text
str subclass
int subclass
float subclass
UUID object
Enum
bytes
Path
opaque object
```

Each must be rejected at construction.

## 34. ID-native child traversal test

Create P and C with `C.parent_resource_id = P.resource_id`, stale legacy metadata, and assert `children_of(P.resource_id)` contains C.

## 35. Parent relocation test

Create P/C, relocate P, verify C relation persists, remove P, reconcile by P ID, release absent C and P, and avoid false retention.

## 36. `set_parent()` test

Create unparented C, call `set_parent(C, P)`, and verify `children_of(P)` sees C immediately.

## 37. Legacy disagreement test

Deliberately stale legacy tuple must not affect ID-native traversal/reconciliation.

## 38. Child relocation test

Preserve external-child retention behavior.

## 39. Literal seven-scenario gate

Development override must produce all seven PASS, PASS/0, non-authoritative reason, DONE stage, successful publication, and no retained execution resources.

## 40. Controlled scenario failure

Require `SCENARIO_FAILURE`/4, truthful publication/cleanup, DONE, and no unintended retained execution resources.

## 41. Literal parent-relocation adversarial acceptance

Run a real runner-level resource lifecycle probe with a physically relocated parent and real child. Reconciliation must use parent resource ID and avoid false retention.

## 42. Literal same-prefix regression

Re-run real same-prefix sibling case and preserve non-PASS.

## 43. Literal partial-dump regression

Re-run partial Node dump and preserve same resource ID through failure.

## 44. Full verification

Run sequentially with no competing pytest:

```bash
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest scripts/tests -q
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest -q
.venv/bin/ruff check .
```

Also run Central/Node checks and migration drift. Require zero unexpected skips.

## 45. Final audit

Require zero gate/restore DBs, zero gate DB connections/processes, zero implementation/reviewer private keys, zero llpkg/workspaces/dumps/logs/unintended temp roots, only intended sanitized evidence, no credential/answer/private artifacts, and clean working tree.

---

## 46. Expected scope

Likely implementation files:

```text
scripts/release_gate/evidence.py
scripts/release_gate/gate_runner.py
scripts/release_gate/e2e_support.py
scripts/release_gate/scenarios.py
scripts/tests/test_release_gate_corrective_v13_*.py
```

Existing tests may be mechanically adapted only when interface changes require it. Do not weaken prior security assertions.

---

## 47. Completion criteria

V13 is complete only when:

- `GateRunResult.evidence` is exactly `ReleaseEvidence | None`;
- mutable/custom evidence references are rejected;
- terminal path fields contain canonical safe strings/None, not retained Path identity;
- all supporting frozen terminal dataclasses validate every field at construction;
- raw UUID/Enum/custom/scalar-subclass identities cannot survive terminal truth;
- `PassDecision`, `ScenarioResult`, and `OutboxInboxProof` are sealed;
- `parent_resource_id` is authoritative;
- `children_of()` traverses by parent resource ID;
- parent relocation preserves child relationship;
- `set_parent()` updates the ID-native graph immediately;
- stale legacy metadata cannot affect traversal/reconciliation/PASS;
- real lifecycle operations use stable IDs where correctness depends on them;
- same-prefix and partial-dump boundaries remain closed;
- seven-scenario gate passes;
- full suite passes;
- independent v13 review returns PASS.

---

## 48. STOP rule

After implementation:

**STOP.**

Request independent Task 15 Task 11 Corrective v13 review.

Do not start Task 12 until the coordinator receives:

```text
A. TASK 15 TASK 11 CORRECTIVE PASS
```

---

## 49. Spec self-review

Completed before handoff:

- no Task12/T15-D28 scope leakage;
- no production model/provider/Task14/T15-D27 changes;
- `GateRunResult.evidence` closed union explicit;
- Path issue solved without impossible exact `Path` type requirement;
- supporting terminal dataclass sealing explicit;
- UUID/Enum/custom-object handling explicit;
- `parent_resource_id` sole authoritative relation;
- legacy tuple demoted to compatibility/recovery metadata;
- parent relocation acceptance explicit;
- call-path migration limited to lifecycle-dependent locations;
- same-prefix and partial-dump CLOSED boundaries preserved;
- full regression and STOP gate explicit;
- no TODO/TBD/placeholders;
- no unresolved design ambiguity.
