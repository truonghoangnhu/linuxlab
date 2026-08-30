# Task 13 Offline Node Runtime and LAN Serving Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement an offline Node runtime that resolves learner content only from one explicitly ACTIVE installed package, pins one immutable package context per request/use-case, returns deep-immutable DTOs, keeps grading server-only, and serves the authoritative LAN route through a thin `linuxlab.sync` HTTP adapter.

**Architecture:** Add a plain-Python `linuxlab.runtime` package above the immutable Task 12 `Installed*` persistence models. A `RuntimePackageContext` pins one exact ACTIVE package when opened; every subsequent lookup rebinds the context to persisted package identity by local PK but never re-checks ACTIVE, so a request remains version-consistent if the package is archived mid-request. `linuxlab.sync.views` is only an HTTP adapter: it opens one context, calls learner-safe runtime services, maps runtime domain exceptions, and renders templates without direct runtime ORM resolution.

**Tech Stack:** Python 3.12, Django 5.2, PostgreSQL, Django templates, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-30-task13-node-runtime-design.md`

**Note on this copy:** this file mirrors the coordinator-supplied plan, with two mechanical adjustments applied so its code sketches match repository reality rather than pseudocode: (1) DTO field names use `activity_public_id` / `question_public_id`, matching the approved spec (§9) and the underlying `InstalledActivityVersion.activity_public_id` / `InstalledQuestionVersion.question_public_id` model fields exactly, instead of the shorthand `public_id`; (2) package lifecycle state is compared against the module-level string constants in `linuxlab.sync.models` (`STAGED`/`ACTIVE`/`ARCHIVED`), not an `InstalledPackage.State` enum, since Task 12 does not define one. No semantic/task-breakdown change was made.

## Global Constraints

- Start from docs HEAD `e7b2e5d`; accepted executable baseline remains Task 12 commit `6e8d9545a5c4507a5c21eb17f55a0332efd53747`.
- Existing baseline is 513 passing tests.
- Preserve decisions `T13-D01` through `T13-D15` in `docs/devlog/2026-08-30-task13-runtime-decisions.md`.
- Node runtime reads only Task 12 installed package/artifact rows. No fallback to Wagtail, `PublicationSnapshot`, `ActivityVersion`, `QuestionVersion`, Central DB, or authoring/domain tables.
- Opening a context requires one explicit ACTIVE package. Never choose highest/latest installed version.
- A pinned context remains valid if its package later becomes ARCHIVED.
- Every lookup rebinds the context by internal local package PK and compares `package_type`, `source_public_id`, `package_version`, and `payload_sha256`; it does not re-check ACTIVE.
- Runtime services return frozen DTOs containing deep-immutable JSON values; no Django model instances leave the runtime boundary.
- Learner-safe content and server grading remain separate. The learner HTTP route must never call the grading service or expose `answer_key`/`scoring`.
- `linuxlab/runtime/` is a plain Python package, not a Django app.
- `linuxlab/sync/views.py`, `urls.py`, and templates are a thin Node HTTP adapter.
- One HTTP request opens and reuses exactly one `RuntimePackageContext`.
- No long-running request transaction / REPEATABLE READ.
- No new database migration is expected.
- No Task 14 Outbox/Inbox/sync transport work.
- If checked-in `TASK_13_offline-node-serving.md` materially conflicts with this approved spec, STOP and report before implementation.

---

### Task 1: Runtime exceptions, deep-freeze utility, and DTO contracts

**Files:**
- Create: `linuxlab/runtime/__init__.py`
- Create: `linuxlab/runtime/exceptions.py`
- Create: `linuxlab/runtime/freeze.py`
- Create: `linuxlab/runtime/dto.py`
- Create: `linuxlab/runtime/tests/__init__.py`
- Create: `linuxlab/runtime/tests/test_dto.py`

**Interfaces:**
- Produces:
  - `RuntimeContentUnavailable`
  - `RuntimeArtifactNotFound`
  - `RuntimeContextInvalid`
  - `freeze_json_value(value)`
  - `RuntimePackageContext`
  - `RuntimePublication`
  - `RuntimeActivity`
  - `RuntimeQuestion`
  - `RuntimeQuestionGrading`
- `linuxlab.runtime.__init__` must not export `RuntimeQuestionGrading` or grading lookup functions as part of the learner facade.

- [x] **Step 1: Write failing tests for recursive immutability and DTO shape**

```python
from dataclasses import FrozenInstanceError
from types import MappingProxyType
from uuid import uuid4

import pytest

from linuxlab.runtime.dto import RuntimeActivity
from linuxlab.runtime.freeze import freeze_json_value


def test_freeze_json_value_recursively_freezes_mapping_and_lists():
    frozen = freeze_json_value(
        {"items": [{"name": "x", "flags": [True, False]}]}
    )

    assert isinstance(frozen, MappingProxyType)
    assert isinstance(frozen["items"], tuple)
    assert isinstance(frozen["items"][0], MappingProxyType)
    assert frozen["items"][0]["flags"] == (True, False)

    with pytest.raises(TypeError):
        frozen["new"] = "value"


def test_runtime_activity_is_frozen_and_nested_config_is_immutable():
    activity = RuntimeActivity(
        activity_public_id=uuid4(),
        version=1,
        type_key="assessment",
        config=freeze_json_value({"questions": [{"version": 1}]}),
    )

    with pytest.raises(FrozenInstanceError):
        activity.version = 2

    with pytest.raises(TypeError):
        activity.config["questions"] = ()
```

- [x] **Step 2: Run focused tests and verify RED**

Run:

```bash
.venv/bin/pytest linuxlab/runtime/tests/test_dto.py -v
```

Expected: import/module failures because runtime package does not exist yet.

- [x] **Step 3: Implement exceptions, recursive freezer, and frozen DTOs**

Use a recursive JSON freezer with these semantics:

```python
from types import MappingProxyType


def freeze_json_value(value):
    if isinstance(value, dict):
        return MappingProxyType(
            {key: freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_json_value(item) for item in value)
    return value
```

Define frozen DTOs with immutable scalar identities and frozen JSON payloads:

```python
from dataclasses import dataclass
from typing import Mapping, Any
from uuid import UUID


@dataclass(frozen=True)
class RuntimePackageContext:
    package_pk: int
    package_type: str
    source_public_id: UUID
    package_version: int
    payload_sha256: str


@dataclass(frozen=True)
class RuntimePublication:
    kind: str
    source_public_id: UUID
    version: int
    content_hash: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class RuntimeActivity:
    activity_public_id: UUID
    version: int
    type_key: str
    config: Mapping[str, Any]


@dataclass(frozen=True)
class RuntimeQuestion:
    question_public_id: UUID
    version: int
    type_key: str
    content: Mapping[str, Any]


@dataclass(frozen=True)
class RuntimeQuestionGrading:
    question_public_id: UUID
    version: int
    answer_key: Mapping[str, Any]
    scoring: Mapping[str, Any]
```

If actual JSON top-level values in current Task 12 schema can legally be non-mapping, type annotations may use a recursive immutable JSON alias, but do not weaken runtime immutability.

- [x] **Step 4: Run tests and verify GREEN**

```bash
.venv/bin/pytest linuxlab/runtime/tests/test_dto.py -v
.venv/bin/ruff check linuxlab/runtime
```

- [x] **Step 5: Commit**

```bash
git add linuxlab/runtime
git commit -m "feat: add immutable runtime contracts"
```

---

### Task 2: Pin and validate one ACTIVE runtime package context

**Files:**
- Create: `linuxlab/runtime/context.py`
- Create: `linuxlab/runtime/tests/test_context.py`
- Modify: `linuxlab/runtime/__init__.py`

**Interfaces:**
- Consumes: Task 12 `InstalledPackage`.
- Produces:
  - `open_runtime_context(package_type, source_public_id) -> RuntimePackageContext`
  - private `_rebind_runtime_context(context) -> InstalledPackage`
- `_rebind_runtime_context` must compare persisted identity to the context and must not require `state == ACTIVE`.

- [x] **Step 1: Write RED tests for ACTIVE-only opening, no-latest fallback, forged context rejection, and archive survival**

Tests must create installed packages through the existing trusted Task 12 import path (`import_package()`/`activate_package()`), not public ORM fabrication.

Required behavior:

```python
def test_open_runtime_context_requires_active_package(...):
    # imported STAGED package exists but is not active
    with pytest.raises(RuntimeContentUnavailable):
        open_runtime_context("learning_path", source_uuid)


def test_open_runtime_context_pins_exact_active_identity(...):
    activate_package(v1)
    context = open_runtime_context("learning_path", source_uuid)

    assert context.package_pk == v1.pk
    assert context.package_version == v1.package_version
    assert context.payload_sha256 == v1.payload_sha256


def test_rebind_rejects_fabricated_identity_claim(...):
    context = open_runtime_context("learning_path", source_uuid)
    forged = replace(context, payload_sha256="0" * 64)

    with pytest.raises(RuntimeContextInvalid):
        _rebind_runtime_context(forged)


def test_pinned_context_remains_valid_after_package_is_archived(...):
    activate_package(v1)
    context = open_runtime_context("learning_path", source_uuid)

    activate_package(v2)

    rebound = _rebind_runtime_context(context)
    assert rebound.pk == v1.pk
    assert rebound.state == ARCHIVED  # linuxlab.sync.models.ARCHIVED constant
```

Also verify a newly opened context after v2 activation resolves v2.

- [x] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest linuxlab/runtime/tests/test_context.py -v
```

- [x] **Step 3: Implement context open/rebind**

`open_runtime_context()`:
- queries exact `package_type + source_public_id + state=ACTIVE`;
- returns `RuntimeContentUnavailable` if none;
- never sorts/selects highest version as fallback;
- builds a frozen context from persisted package identity.

`_rebind_runtime_context()`:
- reloads by `package_pk`;
- if row no longer exists, raise `RuntimeContextInvalid`;
- compare exactly:
  - package_type
  - source_public_id
  - package_version
  - payload_sha256
- mismatch -> `RuntimeContextInvalid`;
- DO NOT check package state.

- [x] **Step 4: Run context and Task 12 regression tests**

```bash
.venv/bin/pytest linuxlab/runtime/tests/test_context.py linuxlab/sync/tests -v
```

- [x] **Step 5: Commit**

```bash
git add linuxlab/runtime
git commit -m "feat: pin active runtime package context"
```

---

### Task 3: Learner-safe publication, activity, and question runtime services

**Files:**
- Create: `linuxlab/runtime/content.py`
- Create: `linuxlab/runtime/tests/test_content.py`
- Modify: `linuxlab/runtime/__init__.py`

**Interfaces:**
- Consumes: `RuntimePackageContext`, `_rebind_runtime_context`, Task 12 installed artifact models.
- Produces:
  - `get_runtime_publication(context) -> RuntimePublication`
  - `get_runtime_activity(context, activity_public_id, version) -> RuntimeActivity`
  - `get_runtime_question(context, question_public_id, version) -> RuntimeQuestion`

- [x] **Step 1: Write RED tests for exact package scoping and DTO isolation**

Required cases:

```python
def test_publication_returns_dto_not_django_model(...):
    ctx = open_runtime_context(...)
    publication = get_runtime_publication(ctx)

    assert isinstance(publication, RuntimePublication)
    assert not isinstance(publication, InstalledPublication)


def test_activity_lookup_is_scoped_to_pinned_package(...):
    ctx_v1 = open_runtime_context(...)
    activate_package(v2)

    activity = get_runtime_activity(ctx_v1, activity_uuid, activity_v1)
    assert activity.version == activity_v1


def test_activity_from_other_package_is_not_visible(...):
    with pytest.raises(RuntimeArtifactNotFound):
        get_runtime_activity(ctx_v1, only_v2_uuid, only_v2_version)


def test_question_dto_contains_no_answer_key_or_scoring(...):
    question = get_runtime_question(ctx, question_uuid, 1)

    assert not hasattr(question, "answer_key")
    assert not hasattr(question, "scoring")
    assert "correct_option_id" not in repr(question.content)
```

Also test nested DTO payload/config/content cannot be mutated.

- [x] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest linuxlab/runtime/tests/test_content.py -v
```

- [x] **Step 3: Implement content services**

Every function:
1. calls `_rebind_runtime_context(context)`;
2. queries only artifacts owned by that exact installed package PK;
3. uses exact UUID/version lookups;
4. never queries Central/Wagtail/domain authoring models;
5. on absence raises `RuntimeArtifactNotFound`;
6. converts JSON through `freeze_json_value`;
7. returns frozen DTO only.

Publication lookup should expect exactly the package-owned one-to-one publication and fail closed with `RuntimeArtifactNotFound` if missing.

- [x] **Step 4: Run learner-runtime suite**

```bash
.venv/bin/pytest linuxlab/runtime/tests/test_context.py linuxlab/runtime/tests/test_content.py -v
.venv/bin/ruff check linuxlab/runtime
```

- [x] **Step 5: Commit**

```bash
git add linuxlab/runtime
git commit -m "feat: resolve learner content from pinned package"
```

---

### Task 4: Separate server-only grading runtime service

**Files:**
- Create: `linuxlab/runtime/grading.py`
- Create: `linuxlab/runtime/tests/test_grading.py`
- Verify: `linuxlab/runtime/__init__.py` does not export grading service by default

**Interfaces:**
- Produces:
  - `get_runtime_question_grading(context, question_public_id, version) -> RuntimeQuestionGrading`
- Grading service is imported explicitly from `linuxlab.runtime.grading`, not learner facade.

- [x] **Step 1: Write RED tests for grading isolation**

```python
def test_grading_is_available_only_through_grading_service(...):
    grading = get_runtime_question_grading(ctx, question_uuid, 1)

    assert isinstance(grading, RuntimeQuestionGrading)
    assert grading.answer_key["correct_option_id"] == expected_id


def test_grading_is_deeply_immutable(...):
    grading = get_runtime_question_grading(ctx, question_uuid, 1)

    with pytest.raises(TypeError):
        grading.answer_key["correct_option_id"] = "other"


def test_runtime_facade_does_not_export_grading_lookup():
    import linuxlab.runtime as runtime

    assert not hasattr(runtime, "get_runtime_question_grading")
```

- [x] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest linuxlab/runtime/tests/test_grading.py -v
```

- [x] **Step 3: Implement grading service**

Rebind context exactly as learner services do. Query the grading row through the installed question owned by the exact pinned package. Return `RuntimeArtifactNotFound` when question/grading is absent. Deep-freeze `answer_key` and `scoring`. Do not add a combined learner+grading DTO.

- [x] **Step 4: Run runtime core suite**

```bash
.venv/bin/pytest linuxlab/runtime/tests -v
```

- [x] **Step 5: Commit**

```bash
git add linuxlab/runtime
git commit -m "feat: add isolated runtime grading lookup"
```

---

### Task 5: Thin Node LAN HTTP route backed only by runtime services

**Files:**
- Create: `linuxlab/sync/views.py`
- Create: `linuxlab/sync/urls.py`
- Create: template path required by checked-in Task 13 prompt under `linuxlab/sync/templates/...`
- Create/modify: HTTP tests under the existing sync test package
- Modify: project URL configuration only as required by the existing Central/Node URL convention

**Interfaces:**
- Produces authoritative learner route conceptually:
  - `GET /learn/<uuid:source_public_id>/`
- HTTP adapter consumes only learner-safe runtime facade:
  - `open_runtime_context`
  - `get_runtime_publication`
  - optionally activity/question functions only when the authoritative template requires them
- It must not import `linuxlab.runtime.grading`.

- [x] **Step 1: Inspect authoritative route and existing URL/profile conventions**

Run:

```bash
sed -n '1,240p' docs/codex/task-prompts/TASK_13_offline-node-serving.md
grep -R "ROOT_URLCONF\|urlpatterns\|LINUXLAB_PROFILE\|LINUXLAB_CAN_" -n linuxlab | head -200
```

If the checked-in prompt requires an exact template name/status code/context shape, implement that exact contract. If it materially contradicts T13-D01–D15, STOP rather than reinterpret. (The checked-in prompt does not specify exact status codes/template names; this plan documents the chosen contract in the view/tests directly.)

- [x] **Step 2: Write RED HTTP tests**

Required semantics:

```python
def test_node_learn_route_renders_active_installed_publication(client, ...):
    response = client.get(f"/learn/{source_uuid}/")
    assert response.status_code == 200


def test_learn_route_does_not_use_staged_latest_package(client, ...):
    # v1 ACTIVE, v2 higher version but STAGED
    response = client.get(f"/learn/{source_uuid}/")
    assert response.context["runtime_context"].package_version == v1.package_version


def test_no_active_package_returns_offline_unavailable_response(client, ...):
    response = client.get(f"/learn/{source_uuid}/")
    assert response.status_code == 503  # authoritative unavailable status chosen for this route


def test_learner_route_context_has_no_grading_material(client, ...):
    response = client.get(f"/learn/{source_uuid}/")
    rendered = response.content.decode()
    assert "answer_key" not in rendered
    assert "correct_option_id" not in rendered
```

Central-profile test must prove the authoritative Task 13 rejection behavior using the real profile flag/convention (a new `LINUXLAB_CAN_SERVE_LEARNER_CONTENT` capability flag, following the existing `LINUXLAB_CAN_IMPORT_PACKAGES` pattern).

- [x] **Step 3: Implement the thin view and URL**

The view must:
1. profile-gate according to checked-in Task 13 behavior;
2. call `open_runtime_context(...)` exactly once;
3. reuse that context for all runtime lookups during the request;
4. call only learner-safe runtime functions;
5. map:
   - `RuntimeContentUnavailable` -> authoritative offline-unavailable response/template (503);
   - `RuntimeArtifactNotFound` -> authoritative not-found response (404);
   - `RuntimeContextInvalid` -> fail-closed runtime/server response (500);
6. render template data from DTOs only.

Forbidden in `views.py`:
- querying `InstalledPackage` or any `Installed*` artifact for runtime resolution;
- Wagtail/Central/domain fallback;
- highest/latest selection;
- importing grading service.

- [x] **Step 4: Implement minimal template(s)**

Templates must render only learner-safe DTO data required by the checked-in Task 13 prompt. Do not serialize grading. Do not add React/SPA work.

- [x] **Step 5: Run HTTP tests**

```bash
.venv/bin/pytest linuxlab/sync/tests -v
```

- [x] **Step 6: Commit**

```bash
git add linuxlab/sync linuxlab/urls.py linuxlab/settings
git commit -m "feat: serve active Node content over LAN"
```

Use the actual URL configuration path discovered in Step 1; do not modify unrelated URL files.

---

### Task 6: Prove request pinning across activation and total offline operation

**Files:**
- Modify: runtime tests
- Modify: sync HTTP tests
- No production code unless a test exposes a real defect

**Interfaces:**
- Proves T13-D08, D10, D11, D12, D14, D15 end-to-end.

- [ ] **Step 1: Add request/use-case pinning test**

Runtime-level sequence:

```python
activate_package(v1)
ctx = open_runtime_context(package_type, source_uuid)

pub_v1 = get_runtime_publication(ctx)

activate_package(v2)

activity_v1 = get_runtime_activity(ctx, activity_uuid_v1, activity_version_v1)
new_ctx = open_runtime_context(package_type, source_uuid)

assert pub_v1.version == v1.publication_version  # InstalledPublication.version, not InstalledPackage.package_version
assert activity_v1 matches v1
assert new_ctx.package_pk == v2.pk
```

The exact publication/version assertion must use the actual installed schema; do not conflate package version with publication version if they are separate fields.

- [ ] **Step 2: Add no-network HTTP test**

Patch outbound network entry points to fail loudly:

```python
def deny_network(*args, **kwargs):
    raise AssertionError("Task 13 runtime attempted network access")

monkeypatch.setattr(socket, "create_connection", deny_network)
monkeypatch.setattr(socket.socket, "connect", deny_network)
```

Then request the LAN learner route. Expected: successful offline rendering from local installed artifacts.

Patch only functions that do not interfere with Django's in-process test client/database connection.

- [ ] **Step 3: Add no-fallback regression test**

Monkeypatch or otherwise make Central/domain fallback paths explode if touched, then resolve/render through Task 13. The test should prove runtime imports/services never require:
- Wagtail pages;
- PublicationSnapshot;
- ActivityVersion;
- QuestionVersion.

Prefer import/query isolation assertions over brittle global monkeypatching.

- [ ] **Step 4: Run Task 13 focused tests**

```bash
.venv/bin/pytest linuxlab/runtime/tests linuxlab/sync/tests -v
```

- [ ] **Step 5: Commit**

```bash
git add linuxlab/runtime/tests linuxlab/sync/tests
git commit -m "test: prove offline pinned runtime serving"
```

---

### Task 7: Documentation implementation record and boundary audit

**Files:**
- Modify: `docs/devlog/2026-08-30-task13-runtime-decisions.md`
- Optionally modify spec only to record implementation filenames if no semantics change
- No new architectural decision without human approval

**Interfaces:**
- Records implementation result without silently changing T13-D01–D15.

- [ ] **Step 1: Audit dependency direction**

Run:

```bash
grep -R "InstalledPackage\|InstalledPublication\|InstalledActivityVersion\|InstalledQuestionVersion\|InstalledQuestionGrading" -n linuxlab/sync/views.py linuxlab/sync/urls.py
grep -R "runtime.grading\|get_runtime_question_grading" -n linuxlab/sync
grep -R "PublicationSnapshot\|ActivityVersion\|QuestionVersion\|wagtail" -n linuxlab/runtime linuxlab/sync/views.py
```

Expected:
- no direct `Installed*` runtime resolution in view;
- no grading import/call from learner HTTP path;
- no Central/Wagtail fallback in runtime core/view.

Imports used only for type names/tests must be judged semantically, not by grep alone.

- [ ] **Step 2: Append an implementation record to the dev log**

Record:
- implementation commit range;
- actual files;
- runtime package is not a Django app;
- route/template/profile behavior;
- tests proving pinning, deep immutability, grading separation, no network;
- no semantic change to T13-D01–D15.

If implementation uncovers a new architectural choice not already covered, STOP and request human approval instead of adding `T13-D16` unilaterally.

- [ ] **Step 3: Run documentation diff review**

```bash
git diff --check
git diff -- docs/devlog/2026-08-30-task13-runtime-decisions.md docs/superpowers/specs/2026-08-30-task13-node-runtime-design.md
```

- [ ] **Step 4: Commit**

```bash
git add docs/devlog/2026-08-30-task13-runtime-decisions.md
git commit -m "docs: record Task 13 runtime implementation"
```

---

### Task 8: Full verification and completion gate

**Files:**
- No production changes unless verification exposes a real defect.

**Interfaces:**
- Produces final Task 13 implementation report for independent review.

- [ ] **Step 1: Run focused runtime tests**

```bash
.venv/bin/pytest linuxlab/runtime/tests -v
```

- [ ] **Step 2: Run sync tests**

```bash
.venv/bin/pytest linuxlab/sync/tests -v
```

- [ ] **Step 3: Run relevant existing suites**

```bash
.venv/bin/pytest \
  linuxlab/publication/tests \
  linuxlab/catalog/tests \
  linuxlab/content/tests \
  linuxlab/activities/tests \
  linuxlab/assessment/tests \
  linuxlab/quiz/tests \
  linuxlab/learning/tests \
  -v
```

Adjust only if exact test directory names differ in the repository; do not omit equivalent suites.

- [ ] **Step 4: Run complete suite**

```bash
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest -v
```

Expected: at least the accepted 513-test baseline plus Task 13 tests, all passing.

- [ ] **Step 5: Run static/framework checks**

```bash
.venv/bin/ruff check .
```

Run Central and Node:

```bash
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py check
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py makemigrations --check --dry-run

DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py check
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py makemigrations --check --dry-run
```

Expected: no issues and no model changes.

- [ ] **Step 6: Verify scope**

```bash
git status --short
git diff e7b2e5d..HEAD --stat
git diff e7b2e5d..HEAD --name-only
```

Confirm:
- no migration;
- no Task 14 Outbox/Inbox/transport;
- no Task 12 lifecycle redesign;
- no Task 11 package changes;
- no React/SPA.

- [ ] **Step 7: Produce Task 13 implementation report and STOP**

Do not begin Task 14. Independent review is required before Task 13 is closed.
