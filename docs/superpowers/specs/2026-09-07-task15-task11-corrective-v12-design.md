# Task 15 Task 11 Corrective v12 — Constructor-Sealed Terminal Graph and Stable Resource Identity

**Status:** Design approved; written-spec review pending
**Date:** 2026-09-07
**Base HEAD:** `e1a30763ab41517ad54cd5ce64525c52819b7556`
**Scope:** Task 15 Task 11 corrective only
**Task 12:** STOP
**T15-D28:** NO

**Intended repository path:**
`docs/superpowers/specs/2026-09-07-task15-task11-corrective-v12-design.md`

---

## 1. Purpose

This spec supersedes Task 11 Corrective v11.

The v11 independent review closed the major live-runtime and ownership gaps: literal seven-scenario CLI, same-prefix sibling handling, partial-dump reservation, artifact coverage, restore lifecycle, emergency poisoning prevention, resource-class filtering, publication-sealed PASS, durable resend, and run-ID validation all passed.

Three blockers remain:

1. top-level terminal scalar fields in `ReleaseEvidence` / `GateRunResult` can retain scalar-subclass identity and mutate returned truth after publication;
2. terminal export still admits unsupported leaves such as `bytes` and has a passthrough fallback for unknown values;
3. `ResourceRef` identity is still locator-based rather than a stable opaque resource identity, so relocation changes identity and repeated acquisition can silently preserve stale parent metadata.

Corrective v12 closes exactly these three blockers while preserving all v7–v11 CLOSED boundaries.

Core invariants:

> Every authoritative terminal scalar is canonicalized or rejected at object construction; no scalar subclass or unsupported leaf survives into terminal truth.

> Terminal export accepts only the closed JSON-safe terminal algebra and fails closed on every unsupported value.

> Resource identity is stable and opaque for the entire lifecycle; locator and parent relationship are mutable metadata attached to that stable identity.

---

## 2. Non-goals

Corrective v12 does not:

- start Task 12–15;
- create T15-D28;
- change production Django models;
- change provider architecture;
- change `.llpkg` format/signing semantics;
- change Task 14;
- change T15-D27;
- add new release features;
- add global orphan sweeping;
- change release tagging/pushing behavior.

If implementation requires any of the above, STOP.

---

## 3. Preserve v11 CLOSED boundaries

The following remain baseline and must not regress:

- frozen ordinary `GateRunResult`;
- frozen cached emergency results;
- Mapping/MappingProxy value detachment;
- canonical exact-string mapping keys;
- nested scalar-subclass rejection;
- canonical run-ID grammar;
- explicit resource classes: EXECUTION / PUBLICATION / INFRASTRUCTURE;
- same-prefix sibling safety;
- structural parent reconciliation;
- genuine path-containment fallback only as secondary recovery logic;
- output-path reservation before side-effecting producers;
- individual ownership of primary/tampered llpkg, initial/restart/restore logs, Central/Node dumps, including partial dump;
- restore lifecycle;
- workspace recovery;
- publication-sealed PASS;
- no-publication PASS prohibition;
- emergency/CLI no-leak;
- publication circularity fail-closed;
- timestamp `+00:99` rejection;
- durable resend Inbox `1→2` rejection;
- FINALIZE/DONE transitions;
- bundle-retention truth;
- published scenario/finalization failure equality;
- literal seven-scenario dev CLI PASS/0 non-authoritative.

---

# Part A — Constructor-Sealed Terminal Graph

## 4. Canonical terminal scalar algebra

Authoritative terminal scalar values are limited to exact builtins:

```text
TerminalScalar :=
    exact str
  | exact int
  | exact float
  | exact bool
  | None
```

No scalar subclass is accepted.

No raw `bytes`, `bytearray`, `Path`, `UUID`, `Enum`, custom class, or arbitrary object may enter the authoritative terminal graph.

If a schema requires a path/UUID/hash-like value, it must already be converted into its canonical exact-string representation before entering the terminal object.

---

## 5. Constructor sealing for `ReleaseEvidence`

`ReleaseEvidence` must validate/canonicalize every top-level scalar field at construction time.

At minimum audit:

```text
schema_version
cleanup_status
overall
exit_code
```

and every other top-level terminal scalar.

Required behavior:

```text
raw constructor input
→ exact-type canonical validator
→ accepted exact builtin OR construction failure
→ frozen ReleaseEvidence
```

Do not rely on later `validate_for_pass()` or renderer checks to repair unsafe input.

Do not normalize subclasses through `str(value)`, `int(value)`, etc.; reject unsupported/subclass input rather than execute custom conversion behavior.

---

## 6. Constructor sealing for `GateRunResult`

`GateRunResult` must independently validate all of its own terminal scalar fields, including at minimum:

```text
overall
exit_code
stage/status-like values if present
path/string terminal values
terminal_failure fields
```

It must not trust that nested `ReleaseEvidence` is sufficient.

Emergency results use the same constructor and therefore cannot form a special unsafe graph.

---

## 7. Constructor sealing for failure records

All terminal failure records/value objects must be constructed only from exact canonical terminal scalars and recursively canonical terminal values.

Mutable/custom scalar subclasses in:

```text
message
category
code
stage
other diagnostic terminal fields
```

must be rejected at construction.

---

## 8. Full top-level field matrix

Permanent tests must enumerate every top-level scalar terminal field across:

- `ReleaseEvidence`;
- `GateRunResult`;
- terminal/failure records.

For each applicable field, test:

```text
str subclass
int subclass
float subclass
bool-like custom object where applicable
bytes
Path
custom object
```

Expected: rejection at construction boundary unless the field is explicitly represented by another approved canonical type.

---

# Part B — Closed Fail-Closed Freeze/Export Algebra

## 9. Recursive terminal value algebra

The complete authoritative algebra is:

```text
TerminalValue :=
    TerminalScalar
  | tuple[TerminalValue, ...]
  | FrozenMapping[exact str, TerminalValue]
```

Approved frozen terminal dataclasses may exist only if all of their fields recursively satisfy this same algebra.

---

## 10. `_freeze()` must fail closed

Required behavior:

```text
exact safe scalar
→ accept

Mapping with exact-str keys
→ recursively freeze keys/values into a new detached immutable mapping

list/tuple
→ recursively freeze to tuple

set/frozenset only if schema explicitly permits unordered terminal data
→ recursively freeze to deterministic/approved representation

anything else
→ fail closed
```

No unknown object may be returned unchanged.

No raw `bytes` unless an explicit schema encoding is introduced; v12 does not introduce one, therefore raw `bytes` must be rejected.

---

## 11. `_thaw()` / renderer export must fail closed

Required export algebra:

```text
exact safe scalar
→ exact JSON-safe builtin

FrozenMapping
→ fresh dict[str, plain value]

tuple
→ fresh list[plain value]

anything else
→ fail closed
```

There must be no fallback equivalent to:

```python
return value
```

for unsupported terminal values.

---

## 12. Renderer output guarantee

`to_dict()` or equivalent must return only fresh JSON-safe builtins:

```text
str
int
float
bool
None
dict
list
```

Mutating renderer output must not mutate snapshot/result/persisted evidence.

Renderer export must not retain scalar subclass identity, custom object identity, mapping-key identity, or caller-owned container identity.

---

## 13. Published permanence

After successful publication:

```text
persisted JSON
persisted Markdown
returned ReleaseEvidence / GateRunResult
```

must remain semantically aligned even if:

- caller mutates all original input objects;
- caller mutates a `to_dict()` export;
- caller attempts to mutate ordinary/emergency results.

---

# Part C — Stable Resource Identity

## 14. Stable opaque `resource_id`

Resource identity must no longer be `(kind, locator/safe_id)`.

Each active resource has a stable opaque ID:

```text
resource_id
```

assigned once at reservation/acquisition and preserved until final release/transfer.

The ID may be an internal UUID/string/token, but its exact representation is an implementation detail. It must be stable and not derived from mutable locator text.

---

## 15. Resource record model

Conceptually:

```text
ResourceRecord(
    resource_id,
    kind,
    resource_class,
    locator,
    parent_resource_id | None,
)
```

Exact type names may differ.

Semantics:

- `resource_id`: stable identity;
- `kind`: semantic resource kind;
- `resource_class`: EXECUTION / PUBLICATION / INFRASTRUCTURE;
- `locator`: current physical database/path/etc. locator;
- `parent_resource_id`: stable structural relationship.

---

## 16. Ledger storage keyed by stable ID

Primary ledger storage must be keyed by `resource_id`, behaviorally equivalent to:

```text
records_by_id[resource_id] = ResourceRecord
```

Optional secondary indexes may map `(kind, locator)` to resource ID for lookup.

Secondary indexes are not identity.

---

## 17. Acquisition API returns stable ID

Both acquisition and output reservation APIs must return the stable resource ID.

Examples:

```text
workspace_id = ledger.acquire(...)
node_dump_id = ledger.reserve_output(..., parent_resource_id=workspace_id)
```

Subsequent lifecycle operations should use resource IDs rather than reconstructing identity from locator strings.

---

## 18. Duplicate acquisition semantics

Repeated acquisition of the same active locator/kind must never silently preserve stale metadata.

Allowed design choices:

1. reject implicit duplicate acquisition; or
2. perform an explicit deterministic metadata update through a dedicated API.

Preferred v12 rule:

```text
same active locator + kind
→ duplicate acquire rejected
```

If parent attachment is needed later, use explicit operation such as:

```text
set_parent(resource_id, parent_resource_id)
```

However, real producer/reservation paths should normally provide parent ID at initial reservation.

---

## 19. Stable parent relationship

Structural parent relationships use:

```text
parent_resource_id
```

not `(parent_kind, parent_locator)`.

Therefore parent relocation does not invalidate child relationships.

Child traversal:

```text
children where parent_resource_id == parent_id
```

---

## 20. Relocation preserves identity

Relocation updates metadata, not identity:

```text
before:
  resource_id = R42
  locator = /workspace/a.dump

relocate(R42, /outside/a.dump)

after:
  resource_id = R42
  locator = /outside/a.dump
```

Do not implement relocation as `release(old) + acquire(new)`.

Parent relationship after relocation must be explicit:

- preserve current parent if still structurally valid; or
- set a new parent / detach via explicit operation.

No stale parent is allowed.

---

## 21. Parent removal semantics

Authoritative children are found by `parent_resource_id`.

For verified parent removal:

```text
for child in structural children(parent_id):
    verify child's current locator
    if absent:
        release(child.resource_id)
    else:
        keep child owned

then release parent if parent itself is verified absent
```

No path-prefix inference.

---

## 22. Path containment fallback is recovery-only

The existing genuine `Path.resolve()/relative_to()` fallback may remain only as a secondary recovery helper for legacy/test fixtures lacking structural parent metadata.

Rules:

- it cannot create ownership;
- it cannot assign structural parentage;
- it cannot replace stable resource identity;
- it cannot release a resource unless that resource's own current locator is verified absent;
- same-prefix siblings must remain safe;
- symlink/outside-parent cases must remain safe.

---

## 23. Output reservation keeps stable identity

Existing v11 output-path reservation remains, but now returns/stores stable resource IDs.

Lifecycle:

```text
reserve output path
→ stable resource_id assigned
→ producer starts
→ success/partial failure uses same resource_id
→ relocate if path moves
→ release same resource_id after verified absence/transfer
```

The partial Node dump case must preserve the same ID from reservation through partial failure and cleanup.

---

## 24. Artifact coverage remains closed

Do not redesign artifact coverage.

Regression-test existing ownership for:

- primary llpkg;
- tampered llpkg;
- initial Central/Node logs;
- restarted Central/Node logs;
- restored Central/Node logs;
- Central dump;
- Node dump / partial Node dump.

The only v12 change is stable-ID lifecycle underneath these already-covered resources.

---

# Part D — PASS / Cleanup Invariants

## 25. PASS invariant remains unchanged

PASS is possible only if:

```text
no primary failure
no cleanup failure
no finalization failure
no retained EXECUTION resources
validate_for_pass succeeds
publication_outcome == PUBLISHED
```

Stable resource IDs must not weaken this.

---

## 26. Resource retention derives from live records

`execution_resources_retained` derives from active ledger records whose resource class is EXECUTION.

Locator changes do not affect identity or retention classification.

---

## 27. Emergency path remains closed

Constructor sealing must also apply to prebuilt emergency results.

If canonicalization/export unexpectedly fails during terminal collapse:

- no recursion;
- fixed safe emergency result;
- deterministic nonzero;
- no raw traceback/sentinel;
- no evidence publication claim;
- existing primary precedence preserved.

---

# Part E — Mandatory TDD

## 28. RED tests: top-level terminal scalar sealing

For every top-level terminal scalar field across `ReleaseEvidence`, `GateRunResult`, and failure records:

- exact valid builtin passes;
- subclass fails;
- unsupported value fails.

Explicitly include:

```text
cleanup_status str subclass
schema_version int subclass
overall str subclass
exit_code int subclass
failure message str subclass
```

---

## 29. RED tests: bytes / unsupported export

Require:

```text
bytes anywhere in terminal graph
→ construction/freeze reject

opaque custom value
→ reject

unsupported value somehow supplied to thaw/export
→ fail closed
```

No identity passthrough.

---

## 30. RED tests: to_dict exact builtins

Walk the complete exported tree and assert every leaf is exact:

```text
type(v) is str/int/float/bool or v is None
```

and every container is plain `dict` or `list`.

No subclass/custom leaf.

---

## 31. RED tests: stable resource identity

Acquire a resource and assert:

```text
resource_id exists
resource_id independent of locator
```

Relocate it and require:

```text
same resource_id
new locator
```

---

## 32. RED tests: stable parent IDs

Create parent and child using explicit IDs.

Require:

```text
child.parent_resource_id == parent.resource_id
```

Relocate parent; relationship remains intact.

---

## 33. RED tests: duplicate acquisition

Reserve/acquire a locator once, then attempt duplicate acquisition with different parent metadata.

Require either:

- explicit rejection; or
- explicit deterministic update only through dedicated metadata API.

No silent stale parent.

---

## 34. RED tests: partial dump stable ID

Reserve Node dump before producer.

Inject partial write failure.

Require:

```text
same resource_id before producer and after failure
locator unchanged unless explicitly relocated
resource remains owned
```

---

## 35. RED tests: same-prefix and relocation regressions

Re-run:

- same-prefix sibling outside parent;
- true child;
- child moved outside then explicit relocate;
- parent cleanup after relocation.

Require no false release and no PASS with surviving execution resource.

---

# Part F — Acceptance / Regression

## 36. Literal seven-scenario CLI acceptance

Run shipped/real release-gate entrypoint:

- macOS normal path => exit 2;
- permitted development override => all seven real scenarios;
- PASS/0;
- authoritative=false;
- reason=`development_only_platform`;
- publication succeeds;
- stage DONE;
- no retained execution resources;
- evidence parity holds.

---

## 37. Literal adversarial acceptance

Mandatory:

### Same-prefix sibling

- sibling survives parent cleanup;
- stable resource ID remains owned;
- execution retention true;
- non-PASS.

### Partial Node dump

- stable resource ID reserved before producer;
- partial output exists at failure;
- same ID remains owned;
- cleanup truth accurate;
- retained execution resource blocks PASS if cleanup deliberately fails.

---

## 38. Full verification

On exact final HEAD:

```bash
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest scripts/tests -q
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest -q
.venv/bin/ruff check .
```

Also run:

- Central `manage.py check`;
- Node `manage.py check`;
- Central migration drift check;
- Node migration drift check;
- literal macOS refusal;
- literal seven-scenario dev CLI;
- controlled scenario failure;
- canonical terminal adversarial probes;
- stable-ID relocation probes;
- same-prefix adversarial run;
- partial-dump adversarial run.

Run real-infra suites sequentially with no competing pytest/gate process.

---

## 39. Regression boundaries

Must remain green:

- Tasks 7–10;
- target safety;
- private-key permissions;
- transactional keypair;
- partial setup;
- descriptor ownership;
- sensitive argv;
- ActionError;
- N1;
- N3;
- restart;
- authoring;
- backup/restore;
- Task14;
- T15-D27;
- `.llpkg` unchanged;
- v7 emergency/no-publication/durable resend;
- v8 restore/published evidence equality;
- v9 canonical run-ID;
- v10 frozen emergency/resource classes;
- v11 canonical mapping keys, structural path safety, artifact coverage, output reservation.

---

## 40. Expected scope

Likely Task 11-only files:

- `scripts/release_gate/evidence.py`
- `scripts/release_gate/gate_runner.py`
- `scripts/release_gate/e2e_support.py` only for stable-ID plumbing if needed
- `scripts/release_gate/scenarios.py` only for stable-ID plumbing if needed
- Task 11 tests

No production Django app changes expected.

---

## 41. Completion criteria

Corrective v12 is complete only when:

- every `ReleaseEvidence` top-level scalar is constructor-sealed;
- every `GateRunResult` top-level scalar is constructor-sealed;
- failure-record scalars are constructor-sealed;
- no scalar subclass survives terminal construction;
- raw `bytes` and unsupported values are rejected;
- freeze and thaw/export both fail closed;
- `to_dict()` contains exact JSON-safe builtin leaves only;
- resource identity is stable and opaque;
- locator changes do not change resource identity;
- parent relationships use `parent_resource_id`;
- duplicate acquisition cannot silently preserve stale parent metadata;
- relocation updates locator while preserving resource ID;
- output reservations keep the same resource ID through partial producer failure;
- same-prefix and moved-external resources cannot be falsely released;
- all v7–v11 CLOSED boundaries remain closed;
- full suite, Ruff, checks/drift, and literal CLI acceptance pass on exact final HEAD;
- independent v12 review returns PASS.

---

## 42. STOP rule

After implementation:

**STOP.**

Request independent Task 11 Corrective v12 review.

Do not start Task 12 until the coordinator receives:

```text
A. TASK 15 TASK 11 CORRECTIVE PASS
```

---

## 43. Spec self-review

Completed before handoff:

- no Task 12/T15-D28 scope leakage;
- no production model/provider/Task14/T15-D27 changes;
- no new orphan sweeper;
- no artifact-coverage redesign beyond stable-ID plumbing;
- all top-level scalar terminal boundaries explicitly included;
- raw bytes explicitly excluded from terminal algebra;
- freeze and thaw both fail closed;
- stable resource identity is independent of locator;
- parent relationship uses resource IDs;
- duplicate-acquisition behavior is deterministic and non-silent;
- relocation preserves resource identity;
- path containment remains secondary recovery only;
- literal CLI and adversarial runner acceptance remain mandatory;
- STOP gate and independent review requirement explicit.

No unresolved design placeholder remains.
