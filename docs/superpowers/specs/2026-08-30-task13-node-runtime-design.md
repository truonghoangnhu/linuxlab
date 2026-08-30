# Task 13 — Node Offline Runtime Resolution Design

**Date:** 2026-08-30
**Status:** DESIGN APPROVED IN CHAT — must be written to repo and reviewed before implementation
**Depends on:** Task 12 FINAL PASS at `6e8d9545a5c4507a5c21eb17f55a0332efd53747`
**Baseline:** 513 tests
**Decision log:** `docs/devlog/2026-08-30-task13-runtime-decisions.md`

## 1. Goal

Task 13 introduces the Node runtime read boundary for offline LinuxLab content.

The runtime resolves learner-safe publication/activity/question data and server-only grading data exclusively from the exact installed package explicitly selected as ACTIVE by Task 12.

It must remain correct if package activation changes during a request/use-case.

## 2. Non-goals

Task 13 does not implement:

- package download;
- Central–Node synchronization;
- Outbox/Inbox;
- package activation/rollback redesign;
- new `.llpkg` schema;
- Wagtail serving fallback;
- authoring;
- browser UI;
- HTTP endpoint design;
- browser grading endpoints;
- exam security/kiosk;
- package garbage collection.

## 3. Architectural boundary

Task 13 introduces a Python runtime service layer between higher-level execution/rendering code and Task 12 installed package models.

Normal consumers must not directly depend on `InstalledPackage`, `InstalledPublication`, `InstalledActivityVersion`, `InstalledQuestionVersion`, or `InstalledQuestionGrading` as their runtime contract.

Conceptual flow:

```text
caller/use-case
   ↓
open_runtime_context(...)
   ↓
RuntimePackageContext
   ↓
content service / grading service
   ↓
Installed* rows scoped to exact pinned package
   ↓
deep-immutable DTO
```

## 4. Runtime source-of-truth

The only runtime content source is the installed artifact set belonging to an explicitly ACTIVE package.

`open_runtime_context(package_type, source_public_id)` performs the ACTIVE lookup.

If no ACTIVE package exists, it raises `RuntimeContentUnavailable`.

It must never fall back to:

- Wagtail;
- `PublicationSnapshot`;
- Central/domain `ActivityVersion`;
- Central/domain `QuestionVersion`;
- highest installed version;
- most recently imported package.

This preserves the Task 11/12 chain from signed publication to explicit Node activation.

## 5. RuntimePackageContext

`RuntimePackageContext` is an immutable request/use-case scoped locator and identity claim.

Conceptual contract:

```python
@dataclass(frozen=True)
class RuntimePackageContext:
    package_pk: int
    package_type: str
    source_public_id: UUID
    package_version: int
    payload_sha256: str
```

`package_pk` is local implementation detail only. It must not become public/cross-node identity.

The remaining fields bind the context to the persisted package identity that was ACTIVE when the context was opened.

## 6. Context opening

`open_runtime_context()`:

1. performs an exact ACTIVE lookup by `(package_type, source_public_id)`;
2. does not infer highest/latest version;
3. creates a frozen context from persisted package identity;
4. performs no Central/Internet access;
5. does not expose the Django model to the caller.

The DB-level Task 12 one-ACTIVE-per-source invariant makes this lookup unambiguous.

## 7. Context identity rebind

A frozen dataclass is constructible by ordinary Python code, therefore it is not itself an authorization/trust token.

Every runtime lookup must:

1. load `InstalledPackage` by `context.package_pk`;
2. compare persisted:
   - `package_type`;
   - `source_public_id`;
   - `package_version`;
   - `payload_sha256`;
   against the context values;
3. raise `RuntimeContextInvalid` on any mismatch;
4. scope artifact query to that exact persisted package.

Critically, rebind must **not** require the package state still to be ACTIVE.

## 8. Activation during a request

A context is a logical snapshot of the selected package, not a pointer to the currently ACTIVE package on every read.

Example:

```text
request opens context → v1
admin activates v2
v1 becomes ARCHIVED
same request asks for activity
→ still read v1
```

This is valid because Task 12 guarantees retained immutable archived artifacts.

A new request opens a new context and receives v2.

This avoids mixed-version reads without a long-running database transaction.

## 9. DTO contracts

Runtime services return values, not ORM objects.

Conceptual DTOs:

```python
@dataclass(frozen=True)
class RuntimePublication:
    kind: str
    source_public_id: UUID
    version: int
    content_hash: str
    payload: Mapping[str, object]

@dataclass(frozen=True)
class RuntimeActivity:
    activity_public_id: UUID
    version: int
    type_key: str
    config: Mapping[str, object]

@dataclass(frozen=True)
class RuntimeQuestion:
    question_public_id: UUID
    version: int
    type_key: str
    content: Mapping[str, object]

@dataclass(frozen=True)
class RuntimeQuestionGrading:
    question_public_id: UUID
    version: int
    answer_key: Mapping[str, object]
    scoring: Mapping[str, object]
```

Exact typing may use a recursive immutable JSON type alias if practical.

Do not place answer key/scoring in `RuntimeQuestion`.

## 10. Deep immutable JSON

`@dataclass(frozen=True)` protects field assignment only. It does not protect nested dictionaries/lists.

Task 13 therefore requires a pure helper equivalent to:

```python
freeze_json_value(value)
```

Semantics:

- dict/mapping → immutable mapping whose values are recursively frozen;
- list/tuple-like JSON array → tuple of recursively frozen values;
- string/int/float/bool/None → unchanged immutable scalar;
- unsupported non-JSON runtime values → fail explicitly rather than silently preserve mutable state.

All DTO JSON-bearing fields use this helper before return.

The helper must create a detached immutable value; no DTO should share an authoritative mutable model JSON object.

## 11. Learner content service

Expected learner-safe service operations include equivalents of:

```python
open_runtime_context(package_type, source_public_id)
get_runtime_publication(context)
get_runtime_activity(context, activity_public_id, version)
get_runtime_question(context, question_public_id, version)
```

Each artifact lookup:

1. rebinds/validates the context;
2. filters by the exact pinned package;
3. uses exact UUID + version where applicable;
4. returns a deep-immutable DTO;
5. raises `RuntimeArtifactNotFound` if not contained in that package.

There is no `latest` activity/question lookup.

## 12. Server grading service

Server grading is a separate module/service boundary.

Expected operation:

```python
get_runtime_question_grading(
    context,
    question_public_id,
    version,
)
```

It follows the same context rebind and package scoping rules.

It returns `RuntimeQuestionGrading` only.

Learner/content module must not export grading lookup in its normal facade.

No combined DTO or default query should merge learner content with answer keys/scoring.

## 13. Error model

Use a small explicit runtime-domain hierarchy.

Expected semantics:

### RuntimeContentUnavailable

Raised by context opening when no exact ACTIVE package exists for the requested `(package_type, source_public_id)`.

### RuntimeContextInvalid

Raised when context package locator no longer exists or persisted package identity does not match the context claim.

Do not use this merely because pinned package is now ARCHIVED.

### RuntimeArtifactNotFound

Raised when the exact requested publication/activity/question/grading artifact is not present in the pinned package.

Task 13 must not assign HTTP status codes.

## 14. Package state semantics

Only `open_runtime_context()` requires `ACTIVE`.

Artifact service functions operate on a previously opened context and do not re-check ACTIVE.

This distinction is load-bearing:

```text
new context → current ACTIVE only
existing context → exact pinned package regardless of later ACTIVE→ARCHIVED
```

## 15. Query discipline

Runtime artifact reads must always include the pinned InstalledPackage scope.

Never resolve an activity or question globally by UUID/version and then assume it belongs to the context.

Conceptually:

```text
InstalledActivityVersion
WHERE package_id = pinned_package
AND activity_public_id = ...
AND version = ...
```

Same rule for questions and grading.

## 16. No cross-package leakage

A context pinned to Package A must never retrieve:

- activity belonging only to Package B;
- question belonging only to Package B;
- grading belonging only to Package B.

Even if UUID/version values happen to overlap across package inventory, package ownership is part of every lookup.

## 17. Offline requirement

After Task 12 package import/activation, all Task 13 operations require only:

- local Node PostgreSQL;
- local application code.

No Central DB, Central API, Internet, Wagtail read, package signer, or trusted public key is needed for normal runtime resolution.

## 18. Concurrency model

Do not hold a database transaction open across a request/use-case.

Consistency comes from:

- immutable package artifacts;
- retained archived package rows;
- pinned context identity;
- package-scoped exact lookups.

Activation may occur concurrently and affects only which package future contexts open.

## 19. Security properties

Task 13 must preserve these invariants:

1. learner runtime cannot acquire server grading through learner DTOs;
2. DTO JSON cannot be modified after return;
3. caller-created fake context cannot redirect lookup to an inconsistent identity;
4. a context cannot cross package ownership boundaries;
5. activation during a request cannot mix two package versions;
6. runtime does not accidentally execute unpublished Central/domain state;
7. local database PK is never public/cross-node identity.

## 20. Expected module organization

Preferred:

```text
linuxlab/runtime/
├── __init__.py
├── context.py
├── dto.py
├── freeze.py
├── content.py
├── grading.py
├── exceptions.py
└── tests/
```

However, before implementation Claude must inspect the checked-in Task 13 prompt and current project structure.

If the authoritative prompt explicitly assigns this code to an existing app/module, stop and report the conflict before moving files or creating a new Django app.

A plain Python package is preferred over a new Django app unless persistent models/configuration require one. Task 13 should add no persistence model.

## 21. Database/migrations

Expected: **no new models and no migration**.

Task 13 is a read/runtime boundary over Task 12 installed state.

If implementation appears to require persistent runtime state, stop and justify it before adding schema.

## 22. Testing strategy

Tests must prove behavior, not merely DTO shapes.

Required load-bearing scenarios:

1. no ACTIVE package → `RuntimeContentUnavailable`;
2. ACTIVE package → context pins exact identity;
3. no latest fallback;
4. activate v2 after opening context on v1 → old context still resolves v1;
5. newly opened context resolves v2;
6. fake/mismatched context → `RuntimeContextInvalid`;
7. missing exact activity/question → `RuntimeArtifactNotFound`;
8. package A context cannot retrieve package B artifact;
9. exact activity/question version is respected;
10. returned publication/activity/question JSON is deeply immutable;
11. returned grading JSON is deeply immutable;
12. learner DTO contains no answer_key/scoring;
13. grading obtained only through grading service;
14. runtime performs no Central/Wagtail fallback;
15. ordinary read flow works with ARCHIVED pinned package;
16. no model instance is exposed as the public runtime result.

Prefer a real integration fixture created through accepted Task 11 → Task 12 flow for at least one end-to-end runtime test.

## 23. Architecture compatibility

Task 13 must not change accepted Task 11/12 semantics.

It consumes:

- Task 12 `InstalledPackage` lifecycle and retained artifacts;
- one-ACTIVE-per-source invariant;
- immutable installed artifacts.

It must not bypass Task 12 services to mutate lifecycle.

## 24. Completion gate

Task 13 is complete only when:

- all runtime access goes through the new service boundary in Task 13 scope;
- runtime reads only installed package artifacts;
- contexts pin exact package identity;
- old context remains coherent after activation switch;
- DTOs are deeply immutable;
- grading remains separately accessible server-side only;
- error semantics are explicit;
- no migrations are introduced;
- full existing suite remains green;
- independent code/architecture review returns no Critical/Important findings.

## 25. Deferred work

Track but do not implement now:

- Task 12 Minor: narrow `_insert_trusted()` generic/private interface;
- Task 12 Minor: narrow concurrent-import `IntegrityError` scope;
- real multi-process import concurrency test;
- package GC;
- minimum LinuxLab version enforcement;
- Task 14 synchronization;
- HTTP/API mapping;
- UI/runtime rendering integration beyond what checked-in Task 13 explicitly requires.
