# LinuxLab Dev Log — Task 13 Runtime Decisions

**Date:** 2026-08-30
**Project:** LinuxLab.vn / LinuxLab Open Lab
**Branch context:** `feature/phase1-learning-foundation`
**Accepted predecessor:** Task 12 FINAL PASS
**Task 12 final commit:** `6e8d9545a5c4507a5c21eb17f55a0332efd53747`
**Accepted baseline:** 513 tests

## Status

This document records the architectural decisions explicitly approved before Task 13 implementation.

These decisions are the Task 13 source of truth unless a later dated decision explicitly supersedes one of them.

Do not silently reinterpret or replace these decisions during implementation or review.

---

## D13-01 — Runtime data source

**Decision:** Node runtime reads only from artifacts belonging to the explicitly `ACTIVE` `InstalledPackage`.

Runtime must not fall back to:

- Wagtail pages/revisions;
- `PublicationSnapshot`;
- Central/domain `ActivityVersion`;
- Central/domain `QuestionVersion`;
- authoring tables;
- the numerically highest or most recently imported package.

If no matching ACTIVE package exists, runtime reports content unavailable.

**Reason:** Node must execute the exact verified/published package chosen by the lifecycle layer, not unpublished or differently-versioned local domain data.

---

## D13-02 — Runtime service boundary

**Decision:** Runtime access is exposed through a Python service layer.

Higher-level runtime/web code must not directly query `Installed*` models as its normal contract.

Expected boundary includes operations equivalent to:

- open active runtime context;
- resolve installed publication;
- resolve exact installed activity version;
- resolve exact installed learner question version;
- resolve server grading through a separate grading service.

**Reason:** Keeps ORM/package-layout details behind one stable runtime contract and prevents accidental access to server-only grading data.

---

## D13-03 — Runtime return values

**Decision:** Runtime services return immutable DTOs, not Django model instances and not mutable dictionaries as their public contract.

DTOs use frozen dataclass-style semantics.

Expected conceptual DTOs:

- `RuntimePublication`;
- `RuntimeActivity`;
- `RuntimeQuestion`;
- `RuntimeQuestionGrading`;
- `RuntimePackageContext`.

**Reason:** Runtime callers should consume values, not acquire writable ORM handles.

---

## D13-04 — Deep immutable JSON

**Decision:** Frozen dataclasses are not sufficient by themselves. Nested JSON values must be deep-frozen before crossing the runtime service boundary.

Required conceptual conversion:

- JSON object/dict → immutable Mapping;
- JSON array/list → tuple;
- string/int/float/bool/null → immutable scalar equivalent.

This applies at least to:

- publication payload;
- activity config;
- learner question content;
- grading answer key;
- grading scoring.

No returned DTO may expose authoritative mutable nested JSON.

---

## D13-05 — Learner/server grading separation

**Decision:** Learner/runtime content and server grading use separate DTOs and separate service/module boundaries.

Learner-facing runtime content must not contain:

- `answer_key`;
- `scoring`.

Server grading is obtained only through an explicit grading service returning `RuntimeQuestionGrading`.

The default learner/content facade must not export grading APIs.

---

## D13-06 — Runtime package context

**Decision:** A request/use-case opens one immutable `RuntimePackageContext` at its beginning.

The context pins the exact package that was ACTIVE at context creation time.

Expected identity fields conceptually include:

- local `package_pk` as an internal locator only;
- `package_type`;
- `source_public_id`;
- `package_version`;
- `payload_sha256`.

Local PK is never cross-node/public identity.

---

## D13-07 — No mixed-version runtime

**Decision:** Every artifact lookup during a use-case is scoped to the package pinned by `RuntimePackageContext`.

Services must not independently ask for the current ACTIVE package on every lookup.

This prevents a request from observing, for example:

- publication from package v1;
- activity from package v2;

after an activation occurs between two reads.

---

## D13-08 — Context survives ACTIVE → ARCHIVED transition

**Decision:** Once successfully opened, a `RuntimePackageContext` remains valid for the lifetime of that request/use-case even if the package is later changed from `ACTIVE` to `ARCHIVED`.

Artifact lookup through that context must not re-check that the package is still ACTIVE.

**Reason:** Task 12 retains archived artifacts immutably. Rechecking ACTIVE would destroy request-level snapshot consistency.

---

## D13-09 — Context identity rebind

**Decision:** A runtime context is not trusted solely because it is a frozen Python object.

On every artifact lookup, runtime must reload the installed package using the context's local package locator and verify persisted package identity matches the context claim:

- package type;
- source public UUID;
- package version;
- payload hash.

If persisted identity does not match, fail closed.

Do **not** require the rebound package to remain ACTIVE.

**Reason:** A dataclass can be manually constructed in Python. Context is therefore a locator + identity claim, not a trust token.

---

## D13-10 — Runtime domain errors

**Decision:** Runtime service layer uses explicit domain exceptions rather than ambiguous `None` for materially different failure modes.

Expected semantics:

- no ACTIVE package for requested source → `RuntimeContentUnavailable`;
- context valid but requested exact artifact is not contained in the pinned package → `RuntimeArtifactNotFound`;
- context locator/identity does not match persisted installed package → `RuntimeContextInvalid`.

The runtime core must not map these directly to HTTP status codes. HTTP mapping belongs to the Task 13 HTTP adapter (see T13-D15 below) — the adapter is part of Task 13 itself, architecturally separate from the runtime core, not a distant "later" layer. *(Wording clarified 2026-08-30 alongside T13-D15; see History note below — the exception semantics themselves are unchanged.)*

---

## D13-11 — No long-running DB transaction

**Decision:** Request-level consistency is provided by the pinned package context, not by keeping a PostgreSQL `REPEATABLE READ` transaction open across the whole request.

Each lookup may use ordinary short reads scoped to the pinned immutable package.

---

## D13-12 — Expected runtime package structure

Preferred module structure, subject to checked-in Task 13 prompt/repo conventions:

```text
linuxlab/runtime/
├── __init__.py
├── context.py
├── dto.py
├── freeze.py
├── content.py
├── grading.py
└── exceptions.py
```

Responsibilities:

- `context.py` — open and validate pinned runtime package contexts;
- `dto.py` — frozen runtime DTO contracts;
- `freeze.py` — deep immutable JSON conversion;
- `content.py` — learner-safe publication/activity/question reads;
- `grading.py` — server-only grading reads;
- `exceptions.py` — runtime-domain errors.

If the authoritative checked-in Task 13 prompt requires another placement, implementation must stop and report the conflict rather than silently diverge.

---

## D13-13 — Explicit exclusions

Task 13 must not implement:

- Central fallback;
- latest-version fallback;
- package download/transport;
- Task 14 Outbox/Inbox/event sync;
- browser grading API;
- package signing/verification changes;
- package lifecycle changes unless an actual integration defect is discovered;
- Wagtail runtime fallback;
- React frontend;
- long-running request transaction machinery.

---

## History — HTTP scope correction (2026-08-30)

1. **Decision being superseded:** the initial design spec (`docs/superpowers/specs/2026-08-30-task13-node-runtime-design.md`, §2, as first committed in `0024f82`) listed "HTTP endpoint design" as a Task 13 non-goal.
2. **New decision:** T13-D15, below — Task 13 explicitly includes the authoritative Node LAN HTTP serving route as a thin adapter over the runtime core. "HTTP endpoint design" is no longer a Task 13 non-goal.
3. **Reason:** the checked-in authoritative Task 13 prompt (`docs/codex/task-prompts/TASK_13_offline-node-serving.md`) requires `linuxlab/sync/views.py`, `linuxlab/sync/urls.py`, templates, and a `/learn/<uuid>/` LAN route as the Task 13 deliverable, gated to the Node profile and tested with all outbound network access patched to fail. That deliverable is HTTP-shaped by definition; excluding "HTTP endpoint design" contradicted the authoritative prompt.
4. **Migration/backward-compatibility impact:** none. This is a documentation-only correction — no runtime or HTTP code has been written yet (Task 13 implementation has not started), so nothing existing changes behavior. D13-01 through D13-13 remain fully valid and unmodified in substance; only the one HTTP-status-mapping sentence in D13-10 was reworded in place (see that entry) to remove language that implied HTTP mapping was a distant future concern rather than part of Task 13 itself.
5. **Review approval:** corrective documentation task from the coordinator, 2026-08-30 ("Task 13 Design Documentation Corrective — Align Runtime Core with Authoritative LAN Serving Deliverable").

**Identifier note:** decisions D13-01 through D13-13 above were numbered during the initial spec-writing session. The corrective task that produced T13-D15 restated D13-01 through D13-13's content under its own working labels (T13-D01 through T13-D14) purely for cross-reference while reviewing this document; that restatement confirmed the content already present here was complete and did not request renumbering the existing entries. T13-D15 is the first decision recorded under the `T13-D` prefix and is additive, not a renumbering of the D13-xx series.

---

## T13-D15 — LAN HTTP serving adapter

**Decision:** Task 13 includes the authoritative Node LAN HTTP serving route as a thin adapter over the plain-Python runtime core (D13-01 through D13-13).

Concretely:

- the route lives in the existing `linuxlab.sync` Django app (`views.py`, `urls.py`, templates) — not in `linuxlab/runtime/`, and not in a new Django app;
- the route's responsibilities are limited to: receive the request, call the runtime core, render a template, map runtime domain exceptions to a response, and remain Node-profile-only;
- the route must never perform its own `Installed*` ORM queries for business/runtime resolution, choose a latest version, fall back to Central/domain/Wagtail data, expose grading data, or call `import_package()`/`activate_package()`/`rollback_package()`;
- the runtime core remains framework-light: it must not depend on Django request/response classes, and it must not itself assign HTTP status codes.

**Reason:** the checked-in authoritative Task 13 prompt requires this route as the Task 13 deliverable. Treating "HTTP endpoint design" as a non-goal (the original wording) contradicted that requirement. Keeping the HTTP-specific transport/rendering logic strictly out of the runtime core — rather than abandoning the runtime-core/DTO architecture already approved in D13-02 through D13-13 — preserves that architecture's testability and its independence from any particular delivery mechanism.

---

## Core invariant

```text
Central authoring
    ↓ publish/package
signed .llpkg
    ↓ verify/import
immutable InstalledPackage artifacts
    ↓ explicit ACTIVE selection
open RuntimePackageContext
    ↓ pin exact installed package
context identity rebind on lookup
    ↓
deep-immutable learner DTOs
        OR
separate server grading DTO
```

A package becoming ARCHIVED after context creation does not invalidate that context.

---

## Change control

Any later change to these decisions must be recorded in a dated dev-log entry containing:

1. the decision being superseded;
2. the new decision;
3. the reason;
4. migration/backward-compatibility impact;
5. review approval.

Do not silently change Task 13 runtime semantics in code alone.
