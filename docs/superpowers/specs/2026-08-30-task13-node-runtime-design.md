# Task 13 — Node Offline Runtime Resolution Design

**Date:** 2026-08-30
**Status:** DESIGN APPROVED IN CHAT — must be written to repo and reviewed before implementation
**Depends on:** Task 12 FINAL PASS at `6e8d9545a5c4507a5c21eb17f55a0332efd53747`
**Baseline:** 513 tests
**Decision log:** `docs/devlog/2026-08-30-task13-runtime-decisions.md`
**Correction:** 2026-08-30 — the authoritative checked-in Task 13 prompt requires a LAN HTTP serving route as part of Task 13; the original wording listing "HTTP endpoint design" as a non-goal was superseded (see decision log history note and T13-D15). This revision corrects §2, §3, and adds the HTTP adapter sections; the runtime-core architecture (§4–§19) is unchanged.

## 1. Goal

Task 13 introduces the Node runtime read boundary for offline LinuxLab content, together with the thin Node HTTP adapter that serves that content over the LAN (the checked-in Task 13 deliverable; T13-D15).

The runtime core resolves learner-safe publication/activity/question data and server-only grading data exclusively from the exact installed package explicitly selected as ACTIVE by Task 12. The HTTP adapter calls the runtime core to render that data; it performs no independent business/runtime resolution of its own.

The whole boundary must remain correct if package activation changes during a request/use-case.

## 2. Non-goals

Task 13 does not implement:

- Task 14 synchronization (Outbox/Inbox/event sync);
- Central package download/transport;
- package activation/import/rollback changes, unless an actual integration defect is discovered;
- new `.llpkg` schema or signing/verification changes;
- Wagtail authoring, or a Wagtail runtime fallback;
- browser/learner grading API or endpoints;
- exam security/kiosk;
- package garbage collection;
- latest-version fallback;
- React/frontend SPA work.

**HTTP LAN serving itself IS in Task 13 scope** (§3B, §20–§23; T13-D15) — it is no longer listed as a non-goal. What remains excluded is everything above: richer web/API surface, authentication/authorization redesign, and anything belonging to a later task.

## 3. Architectural boundary

Task 13 has TWO layers.

### 3A. Plain-Python runtime core

`linuxlab/runtime/` is **NOT** a new Django app. It is a plain Python package that provides the runtime/domain read boundary between higher-level execution/rendering code and Task 12 installed package models.

Normal consumers must not directly depend on `InstalledPackage`, `InstalledPublication`, `InstalledActivityVersion`, `InstalledQuestionVersion`, or `InstalledQuestionGrading` as their runtime contract.

### 3B. Thin Node HTTP adapter

The checked-in Task 13 prompt (`docs/codex/task-prompts/TASK_13_offline-node-serving.md`) requires an authoritative LAN HTTP serving route — conceptually `GET /learn/<uuid>/` — backed only by active local package data. This route is implemented as a **thin adapter** living in the existing `linuxlab.sync` app (`views.py`, `urls.py`, templates), per T13-D15. The adapter calls the runtime core in 3A; it must not duplicate runtime resolution rules, and it must not directly query `Installed*` models for business/runtime resolution. Full contract: §20.

### Dependency direction

```text
sync HTTP adapter
        ↓
linuxlab.runtime
        ↓
Installed* persistence models
```

Never:

```text
linuxlab.runtime
        ↓
linuxlab.sync.views
```

and never:

```text
templates/views
        ↓
direct Central authoring models
```

The runtime package must not depend on Django request/response classes, so it stays framework-light and independently testable without an HTTP test client.

### Conceptual flow (runtime core)

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

This preserves the Task 11/12 chain from signed publication to explicit Node activation. This rule applies identically whether the caller is a runtime-core consumer or the HTTP adapter — the HTTP adapter never bypasses it.

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

Use a small explicit runtime-domain hierarchy, owned by the runtime core.

Expected semantics:

### RuntimeContentUnavailable

Raised by context opening when no exact ACTIVE package exists for the requested `(package_type, source_public_id)`.

### RuntimeContextInvalid

Raised when context package locator no longer exists or persisted package identity does not match the context claim.

Do not use this merely because pinned package is now ARCHIVED.

### RuntimeArtifactNotFound

Raised when the exact requested publication/activity/question/grading artifact is not present in the pinned package.

The runtime core must not assign HTTP status codes or otherwise know about HTTP. Mapping these exceptions to a response is entirely the HTTP adapter's job — see §21.

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

## 17. Offline requirement (runtime core)

After Task 12 package import/activation, all Task 13 runtime-core operations require only:

- local Node PostgreSQL;
- local application code.

No Central DB, Central API, Internet, Wagtail read, package signer, or trusted public key is needed for normal runtime resolution. The HTTP adapter inherits this requirement unchanged — see §23 for the HTTP-specific test expectation (network sockets patched to fail).

## 18. Concurrency model

Do not hold a database transaction open across a request/use-case.

Consistency comes from:

- immutable package artifacts;
- retained archived package rows;
- pinned context identity;
- package-scoped exact lookups.

Activation may occur concurrently and affects only which package future contexts open.

## 19. Security properties (runtime core)

Task 13 must preserve these invariants:

1. learner runtime cannot acquire server grading through learner DTOs;
2. DTO JSON cannot be modified after return;
3. caller-created fake context cannot redirect lookup to an inconsistent identity;
4. a context cannot cross package ownership boundaries;
5. activation during a request cannot mix two package versions;
6. runtime does not accidentally execute unpublished Central/domain state;
7. local database PK is never public/cross-node identity.

The HTTP adapter must preserve these too, purely by construction (it only ever touches the runtime core's public surface) — see §22 for the HTTP-specific restatement of invariant 1.

## 20. HTTP adapter contract

The authoritative Node LAN route (conceptually `GET /learn/<uuid>/`) is a thin adapter over the runtime core in §3A–§19. Exact route naming and URL namespace follow the checked-in Task 13 prompt and existing project URL conventions (e.g. the existing `linuxlab.learning.urls` include pattern in `linuxlab/urls.py`).

The adapter's responsibilities are **limited to**:

1. receive request/path parameters;
2. call the runtime service layer (open a context, request the needed DTOs);
3. render the appropriate template from those DTOs;
4. translate runtime domain exceptions into HTTP/UI behavior (§21);
5. remain Node-profile appropriate (§23).

It must **NOT**:

- perform its own `Installed*` ORM queries for business/runtime resolution;
- choose latest versions;
- fall back to Central/domain/Wagtail data;
- expose grading data;
- implement package activation;
- implement package import;
- implement synchronization.

## 21. HTTP request lifecycle, pinned context, and exception mapping

**One context per request.** The view opens exactly one `RuntimePackageContext` via `open_runtime_context()` at the start of request handling, and reuses that same context object for every publication/activity/question lookup needed to render the response. It must not call `open_runtime_context()` more than once per request. This is what prevents a single response from mixing, for example, a publication from v1 with an activity from v2 if an activation happens mid-request (§8, §14, T13-D14).

**ACTIVE → ARCHIVED mid-request.** If the pinned package transitions from ACTIVE to ARCHIVED while a request is in flight (because an admin activated a newer version concurrently), the in-flight request's context remains valid and continues to resolve its originally pinned version to completion — the adapter does not re-open the context and does not re-check ACTIVE (§8, §14). A new request started after that point opens a new context and gets the new ACTIVE version.

**Exception mapping.** The runtime core raises plain Python exceptions (§13) with no HTTP awareness; the adapter maps them:

```text
RuntimeContentUnavailable   → offline/unavailable response (no ACTIVE package for this source)
RuntimeArtifactNotFound     → not-found response
RuntimeContextInvalid       → fail-closed server/runtime error response
```

The runtime core must not hard-code exact status codes or templates. If the checked-in Task 13 prompt specifies exact response codes/templates for these cases, that requirement is recorded and implemented in the adapter, not the runtime core.

**No silent redirect.** On `RuntimeContentUnavailable`, the adapter must not silently redirect to the latest staged/imported package, an archived package, or Central — it renders the offline-unavailable response.

## 22. Learner route security boundary

The learner LAN route must only use learner-safe runtime DTOs: `RuntimePublication`, `RuntimeActivity`, `RuntimeQuestion`.

It must **never** call `get_runtime_question_grading()`, and must never serialize or render `answer_key` or `scoring` into the response, template context, or any client-visible artifact (HTML, headers, embedded JSON).

The grading service (§12) is server-side-only infrastructure reserved for later evaluation flows — Task 13 does not add a learner-facing consumer of it. This is the HTTP-specific restatement of runtime-core invariant 1 in §19.

## 23. Node profile and offline HTTP requirement

**Node-only availability.** The LAN learning route is available only under the Node-capable deployment profile, per the checked-in Task 13 prompt. The Central profile must not expose this route. Gating follows the existing project profile/settings convention already established for Node-capable behavior (see `linuxlab/settings/{base,central,node,test}.py` and the `LINUXLAB_PROFILE`/`LINUXLAB_CAN_*` pattern used for Task 12's `import_package()`); Task 13 does not invent a second deployment codebase or a parallel settings mechanism.

**Offline/no-network requirement.** Serving this route requires no outbound network access at any point: no Central API call, no package download, no remote resource lookup. Task 13 tests must be able to patch socket/network access to fail entirely and still successfully serve installed ACTIVE content through this route, exactly as the checked-in Task 13 prompt requires ("Test a Node serves an active LearningPath while all socket/network connection attempts are patched to fail").

## 24. Expected module organization

Two separate placements, with two separate responsibilities — the corrected Task 13 does **not** require moving all functionality out of `linuxlab.sync`, and does **not** create a new Django app for the runtime core:

**Core architectural responsibility — plain Python package, not a Django app:**

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

**Authoritative HTTP delivery responsibility — inside the existing `linuxlab.sync` Django app:**

```text
linuxlab/sync/
├── views.py
├── urls.py
└── templates/
    └── ...
```

Before implementation, the actual implementation step must still inspect the checked-in Task 13 prompt and current project structure for anything this design did not anticipate (e.g. an existing `linuxlab/sync/tests/` convention for where adapter tests should live). If the authoritative prompt requires yet another placement for the runtime core specifically, implementation must stop and report the conflict rather than silently diverge. A plain Python package remains preferred over a new Django app for the runtime core unless persistent models/configuration require one — and Task 13 adds no persistence model at all (§25).

## 25. Database/migrations

Expected: **no new models and no migration**.

Task 13 — both the runtime core and the HTTP adapter — is a read/runtime boundary over Task 12 installed state. The HTTP adapter adds no models of its own; it only adds `views.py`/`urls.py`/templates.

If implementation appears to require persistent runtime state, stop and justify it before adding schema.

## 26. Testing strategy

Tests must prove behavior, not merely DTO or response shapes.

### Runtime-core tests

1. only an ACTIVE package can open a new context;
2. no ACTIVE → `RuntimeContentUnavailable`;
3. no latest fallback;
4. context identity pinning;
5. fabricated/mismatched context → `RuntimeContextInvalid`;
6. ACTIVE→ARCHIVED context remains readable;
7. a new request/context opened after activation uses the new ACTIVE package;
8. artifact lookup respects exact UUID/version;
9. cross-package artifact lookup is rejected/not found;
10. returned publication/activity/question JSON is deeply immutable;
11. returned grading JSON is deeply immutable;
12. learner DTO contains no `answer_key`/`scoring`;
13. grading is obtainable only through the grading service;
14. runtime performs no Central/Wagtail fallback;
15. no Django model instance is exposed as the public runtime result.

### HTTP adapter tests

1. the authoritative route (e.g. `/learn/<uuid>/`) works on the Node profile;
2. the Central profile rejects/does not expose the route;
3. the route serves the active installed package only;
4. no Central/domain fallback is reachable through the route;
5. switching the ACTIVE package affects only new requests, not one already in flight/rendered from an older pinned context;
6. no `answer_key`/`scoring` appears anywhere in the learner HTML/response/template context;
7. all outbound socket/network calls are patched to fail, and the route still serves successfully;
8. the route/adapter has no dependency on Task 14 code.

Prefer a real integration fixture created through the accepted Task 11 → Task 12 flow for at least one end-to-end runtime-core test, and for at least one end-to-end HTTP adapter test.

## 27. Architecture compatibility

Task 13 must not change accepted Task 11/12 semantics.

It consumes:

- Task 12 `InstalledPackage` lifecycle and retained artifacts;
- one-ACTIVE-per-source invariant;
- immutable installed artifacts.

It must not bypass Task 12 services to mutate lifecycle. The HTTP adapter in particular must never call `import_package()`, `activate_package()`, or `rollback_package()` — it is read-only.

## 28. Completion gate

Task 13 is complete only when:

- all runtime access goes through the runtime-core service boundary in Task 13 scope;
- runtime reads only installed package artifacts;
- contexts pin exact package identity;
- old context remains coherent after an activation switch;
- DTOs are deeply immutable;
- grading remains separately accessible server-side only;
- runtime-core error semantics are explicit and HTTP-unaware;
- the authoritative LAN HTTP route exists, is Node-profile-only, and calls only the runtime core (§20–§23);
- the route works with all outbound network access patched to fail;
- no migrations are introduced;
- full existing suite remains green;
- independent code/architecture review returns no Critical/Important findings.

## 29. Implementation boundary handoff

The future implementation plan for Task 13 covers **both**:

**A. Runtime core**
- context (`open_runtime_context`, identity rebind)
- DTOs (`RuntimePublication`, `RuntimeActivity`, `RuntimeQuestion`, `RuntimeQuestionGrading`, `RuntimePackageContext`)
- deep immutable JSON (`freeze_json_value`)
- learner content services
- grading service
- exceptions (`RuntimeContentUnavailable`, `RuntimeArtifactNotFound`, `RuntimeContextInvalid`)

**B. Thin offline-serving adapter**
- Node URL (`linuxlab/sync/urls.py`)
- view (`linuxlab/sync/views.py`)
- template(s)
- Node-profile restriction
- domain-error → response mapping
- offline/no-network test coverage

This document does not implement either A or B. It records the design for both so a future implementation-planning step can proceed without re-deriving this architecture.

## 30. Deferred work

Track but do not implement now:

- Task 12 Minor: narrow `_insert_trusted()` generic/private interface;
- Task 12 Minor: narrow concurrent-import `IntegrityError` scope;
- real multi-process import concurrency test;
- package GC;
- minimum LinuxLab version enforcement;
- Task 14 synchronization;
- richer HTTP/API mapping beyond the single authoritative LAN route;
- UI/runtime rendering integration beyond what the checked-in Task 13 prompt explicitly requires.
