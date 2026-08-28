# LinuxLab Phase 1 Learning Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first durable LinuxLab release that authors versioned Linux learning content on Central, serves it guest-first, packages only immutable published content, activates it offline on a Node, records learning progress, and synchronizes events idempotently.

**Architecture:** One modular Django/Wagtail monolith with Central and Node deployment profiles. Each Django app owns its models and exposes service interfaces; cross-instance identity uses UUIDs and immutable versions. Central creates signed `.llpkg` artifacts; Node verifies, stages, activates, serves, and later synchronizes local events without database replication.

**Tech Stack:** Python 3.12; Django 5.2 LTS; Wagtail 7.4 LTS; PostgreSQL 16+; Django templates + HTMX + Alpine.js; pytest/pytest-django; Ruff; Ed25519 signatures via `cryptography`; REST/JSON for sync.

**Spec:** `docs/superpowers/specs/2026-08-28-linuxlab-architecture-design.md`

## Global Constraints

- One Django/Wagtail codebase; no Central/Node forks.
- PostgreSQL is used by both Central and Node.
- Central and Node never replicate application databases.
- Node must serve installed content without Internet.
- Modules do not mutate another module's owned models directly.
- UUIDs are cross-instance identities; local PKs are private details.
- Attempts reference immutable Activity versions.
- Wagtail revisions are editorial state; LinuxLab PublishedVersions are learning/sync state.
- Packages are created only from immutable published artifacts.
- Guest learning requires no account; guest progress is local by default.
- Answer keys are never sent to the learner browser.
- No terminal sandbox, formal Exam Engine, React/Vue, Kafka/RabbitMQ, Kubernetes, or microservices in Phase 1.

---

## Planned source layout

```text
linuxlab/
├── settings/{base,central,node,test}.py
├── core/          # stable UUID primitives and domain events
├── identity/      # account, learner, guest, context, roles
├── content/       # Wagtail paths/modules/lessons and blocks
├── activities/    # Activity contract, registry, versions
├── assessment/    # questions, versions, grading
├── quiz/          # assessment Activity provider
├── learning/      # Attempt, Result, Progress
├── publication/   # validation, snapshot, .llpkg, signing
├── sync/          # Node package lifecycle and event sync
└── catalog/       # software/distribution/install recipes
```

### Task 1: Bootstrap Django/Wagtail and Central/Node profiles

**Files:** `pyproject.toml`, `manage.py`, `pytest.ini`, `.env.example`, `linuxlab/settings/*.py`, `linuxlab/urls.py`, `linuxlab/core/tests/test_settings_profiles.py`

**Produces:** `LINUXLAB_PROFILE`; `central`, `node`, and `test` settings modules.

- [ ] Write failing tests asserting `settings.LINUXLAB_PROFILE == "test"` and PostgreSQL engine is configured.
- [ ] Add dependencies with exact floors/ranges: Python `>=3.12,<3.13`, Django `>=5.2,<5.3`, Wagtail `>=7.4,<7.5`, psycopg `>=3.2,<4`, pytest `>=8,<9`, pytest-django `>=4.9,<5`, Ruff `>=0.12,<1`, cryptography `>=45,<46`.
- [ ] Create `base.py`; set `AUTH_USER_MODEL = "identity.Account"`; configure Wagtail and PostgreSQL from environment variables.
- [ ] Create `central.py` with `LINUXLAB_PROFILE="central"`, `LINUXLAB_CAN_AUTHOR=True`, `LINUXLAB_CAN_IMPORT_PACKAGES=False`.
- [ ] Create `node.py` with `LINUXLAB_PROFILE="node"`, `LINUXLAB_CAN_AUTHOR=False`, `LINUXLAB_CAN_IMPORT_PACKAGES=True`.
- [ ] Run `pytest linuxlab/core/tests/test_settings_profiles.py -v` and `manage.py check` under both profiles.
- [ ] Commit `chore: bootstrap LinuxLab Django Wagtail profiles`.

### Task 2: Stable identities and domain event primitive

**Files:** `linuxlab/core/models.py`, `linuxlab/core/events.py`, tests and migration.

**Produces:** `StableIdentityModel.public_id`; `DomainEvent`; `publish_event(event_type, payload, aggregate_id)`.

- [ ] Test that two events receive distinct UUID event IDs and preserve JSON payload.
- [ ] Implement abstract `StableIdentityModel` with unique UUID `public_id`.
- [ ] Implement `DomainEvent(event_id, event_type, aggregate_id, payload, created_at, processed_at)`.
- [ ] Implement `publish_event()` as the only creation helper for general domain events.
- [ ] Migrate; run `pytest linuxlab/core/tests -v`.
- [ ] Commit `feat: add stable identities and domain events`.

### Task 3: Guest-first identity and contextual authorization

**Files:** `linuxlab/identity/models.py`, `services.py`, `policies.py`, tests and migration.

**Produces:** `Account`, `Learner`, `AnonymousLearner`, `LearningContext`, `RoleAssignment`, `ensure_learner()`, `can()`.

- [ ] Test that `Account` and `Learner` have different UUIDs and that `AnonymousLearner` has no Account dependency.
- [ ] Implement a custom `Account(AbstractUser, StableIdentityModel)` from day one.
- [ ] Implement `Learner(account nullable one-to-one)`, `AnonymousLearner`, `LearningContext(kind,name)`, and contextual `RoleAssignment`.
- [ ] Implement `ensure_learner(account)`.
- [ ] Implement `can(actor, action, resource=None, context=None)` using role/action mappings and context scope; do not add `is_teacher`/`is_student` fields.
- [ ] Test that an Instructor role in context A grants access in A and denies the same action in context B.
- [ ] Migrate; run identity tests; commit `feat: add guest-first identity and contextual roles`.

### Task 4: Structured Wagtail learning content

**Files:** `linuxlab/content/blocks.py`, `models.py`, templates, tests and migration.

**Produces:** `LearningPathPage -> ModulePage -> LessonPage`; `CommandBlock`, `TipBlock`, `ActivityReferenceBlock`.

- [ ] Test the allowed Wagtail page hierarchy.
- [ ] Add stable UUID `public_id` to the custom page base.
- [ ] Implement Lesson `StreamField` blocks for heading, rich text, command, tip, and Activity reference `{activity_public_id, activity_version}`.
- [ ] Render Lesson content without reading/writing Progress on page models.
- [ ] Test round-trip serialization of an Activity reference block.
- [ ] Migrate; run content tests; commit `feat: add structured Wagtail learning content`.

### Task 5: Activity contract, registry, and immutable versions

**Files:** `linuxlab/activities/contracts.py`, `registry.py`, `models.py`, `services.py`, tests and migration.

**Produces:** `ActivityProvider` protocol; `ActivityRegistry`; `ActivityDefinition`; `ActivityVersion`; `get_activity_version()`.

- [ ] Test provider registration, duplicate provider rejection, and unknown provider lookup.
- [ ] Define provider methods `validate_config(config)` and `evaluate(config, submission)` for Phase 1; later lifecycle methods extend this without exposing provider internals to Learning.
- [ ] Implement `ActivityDefinition(name,type_key,public_id)`.
- [ ] Implement `ActivityVersion(definition,version,config,required_capabilities,published_at)` with unique `(definition,version)`.
- [ ] Implement publication service so an already-published row is never edited; a change creates version N+1.
- [ ] Test exact-version lookup by definition UUID + version.
- [ ] Migrate; run Activity tests; commit `feat: add versioned activity provider contract`.

### Task 6: Shared Assessment engine and Quiz provider

**Files:** `linuxlab/assessment/{models,providers,services}.py`, `linuxlab/quiz/provider.py`, tests and migration.

**Produces:** `Question`, immutable `QuestionVersion`, single-choice grading, learner-safe serialization, `AssessmentActivityProvider`.

- [ ] Test server-side grading for correct/incorrect single-choice answers.
- [ ] Test that learner serialization contains prompt/options but cannot contain `answer_key` or a `correct` marker.
- [ ] Implement `Question` and versioned `QuestionVersion(type_key,prompt,options,answer_key,scoring,published)`.
- [ ] Implement `SingleChoiceProvider.grade()`.
- [ ] Implement `serialize_for_learner()` as an allow-list, not a model dump.
- [ ] Implement Assessment Activity config as an ordered list of exact `{question_public_id, version}` references.
- [ ] Migrate; run assessment/quiz tests; commit `feat: add shared assessment and quiz provider`.

### Task 7: Attempts, Results, and rebuildable Progress

**Files:** `linuxlab/learning/{models,services,projections}.py`, tests and migration.

**Produces:** `start_attempt()`, `submit_attempt()`, `rebuild_progress()`.

- [ ] Test that Attempt creation rejects unpublished ActivityVersion and stores the exact published version FK.
- [ ] Implement `Attempt(learner,activity_version,started_at,submitted_at,submission)` and immutable `Result(attempt,score,max_score,details)`.
- [ ] Implement `Progress(learner,activity_definition,completed,best_score)` as derived state with a unique learner/activity constraint.
- [ ] Implement `submit_attempt()` through ActivityRegistry, persist Result, and emit `activity.completed` in the same transaction.
- [ ] Implement `rebuild_progress()` from authoritative Results; completion threshold is read from Activity config rather than hard-coded globally.
- [ ] Test deleting Progress then rebuilding yields the same completion/best score.
- [ ] Migrate; run learning tests; commit `feat: add attempts results and rebuildable progress`.

### Task 8: Anonymous local progress and explicit claim flow

**Files:** `linuxlab/learning/static/learning/guest_progress.js`, `views.py`, URLs, tests.

**Produces:** localStorage key `linuxlab.guest.progress.v1`; authenticated `POST /api/v1/learning/claim-progress/`.

- [ ] Implement browser-local anonymous completion storage; no background sync for guests.
- [ ] Test claim endpoint requires authentication.
- [ ] Validate every claimed UUID/version against a published ActivityVersion.
- [ ] Resolve Account -> Learner with `ensure_learner()` and create/rebuild durable progress only for explicitly claimed items.
- [ ] Test unrelated anonymous browser data is not uploaded.
- [ ] Run claim tests; commit `feat: add local guest progress claim flow`.

### Task 9: Publication validation and immutable snapshots

**Files:** `linuxlab/publication/{models,validators,snapshots}.py`, tests and migration.

**Produces:** `PublicationSnapshot`; `validate_learning_path()`; `publish_learning_path()`.

- [ ] Test publication fails when a referenced ActivityVersion or QuestionVersion is unpublished/missing.
- [ ] Implement deterministic canonical JSON and SHA-256 hashing.
- [ ] Implement snapshot model `{kind,source_public_id,version,payload,content_hash,created_at}` with unique version constraint.
- [ ] Resolve exact dependency versions; never serialize `latest`.
- [ ] Test changing a Wagtail draft and republishing creates N+1 without mutating N.
- [ ] Migrate; run publication tests; commit `feat: add immutable publication snapshots`.

### Task 10: Reusable software catalog

**Files:** `linuxlab/catalog/{models,services}.py`, tests and migration.

**Produces:** `Software`, `Distribution`, `InstallationRecipe`, `get_install_command()`.

- [ ] Test one LibreOffice catalog record can return distro-specific install commands.
- [ ] Implement stable UUID models and unique software/distribution recipe relationship.
- [ ] Add service lookup by UUIDs; Lessons reference catalog entities rather than duplicating metadata.
- [ ] Migrate; run catalog tests; commit `feat: add reusable software catalog`.

### Task 11: Signed immutable `.llpkg` format

**Files:** `linuxlab/publication/package.py`, `signing.py`, tests.

**Produces:** `PackageManifest`; `build_package()`; `verify_package()`.

- [ ] Define v1 archive members exactly: `manifest.json`, `payload.json`, `signature.bin`.
- [ ] Manifest fields: schema version, package UUID, package type, package version, created timestamp, payload SHA-256, minimum LinuxLab version, `ed25519` signature algorithm.
- [ ] Build from a PublicationSnapshot only.
- [ ] Sign canonical manifest+payload bytes with Ed25519; Node only needs the public key.
- [ ] Test valid package verifies and a one-byte modification is rejected.
- [ ] Commit `feat: add signed immutable LinuxLab packages`.

### Task 12: Node package verify -> stage -> activate -> rollback

**Files:** `linuxlab/sync/models.py`, `services.py`, tests and migration.

**Produces:** `InstalledPackage`; `import_package()`, `activate_package()`, `rollback_package()`.

- [ ] Test tampered packages are rejected before database payload insertion.
- [ ] Import verified package as `staged` only on a Node-capable profile.
- [ ] Activate transactionally: archive the currently active package of the same type, then activate selected package.
- [ ] Rollback only to a previously verified local immutable package.
- [ ] Test staged update does not change content served until explicit activation.
- [ ] Migrate; run lifecycle/rollback tests; commit `feat: add Node package activation and rollback`.

### Task 13: Offline Node serving

**Files:** `linuxlab/sync/views.py`, `urls.py`, templates, tests.

**Produces:** LAN learning route backed only by active local package data.

- [ ] Test a Node serves an active LearningPath while all socket/network connection attempts are patched to fail.
- [ ] Implement local active-package query service against Node PostgreSQL only.
- [ ] Add `/learn/<uuid>/` Node route rendering package payload.
- [ ] Reject this snapshot-serving route under the Central profile.
- [ ] Run Node test with network blocked; commit `feat: serve active learning packages offline on Node`.

### Task 14: Idempotent Outbox/Inbox event sync

**Files:** `linuxlab/sync/{models,contracts,services,transports,api}.py`, tests and migration.

**Produces:** `OutboxMessage`, `InboxMessage`, `SyncTransport`, HTTP/File transports, `flush_outbox()`, `/api/v1/sync/events/`.

- [ ] Test delivering the same event UUID twice produces one Central processing effect and ACKs both deliveries.
- [ ] Persist OutboxMessage in the same transaction as authoritative local Result/domain event.
- [ ] Implement Central Inbox uniqueness on `event_id` and `get_or_create` processing.
- [ ] Implement retry: an unacknowledged Outbox row remains pending; an ACK marks `acknowledged_at`.
- [ ] Implement `HttpSyncTransport`; implement a deterministic `FileSyncTransport` using exactly the same event envelope for offline/USB-style integration tests.
- [ ] Migrate; run duplicate/retry tests; commit `feat: add idempotent Central Node event sync`.

### Task 15: End-to-end acceptance, CI, and operations docs

**Files:** `tests/e2e/*`, `.github/workflows/test.yml`, `docs/operations/{central,node,package-keys}.md`.

**Produces:** executable proof of the Phase 1 architecture and reproducible operator workflow.

- [ ] E2E: author/publish -> snapshot -> sign `.llpkg` -> import Node -> activate -> disconnect network -> serve learning path.
- [ ] E2E: Node creates local Attempt/Result -> Outbox -> deliver event twice -> Central processes once -> Node records ACK.
- [ ] Run Central and Node against separate PostgreSQL test databases/schemas; never copy application rows directly.
- [ ] CI: Python 3.12 + PostgreSQL 16, install dev dependencies, run `ruff check .`, `pytest`, Central `manage.py check`, Node `manage.py check`.
- [ ] Central operations doc: migrate, create admin, author, publish, signing key generation, package export.
- [ ] Node operations doc: migrate, install trusted public key, import, stage, activate, rollback, LAN bind, offline verification.
- [ ] Key doc: signing private key never ships to Node; key rotation overlaps trusted public keys before old-key retirement.
- [ ] Run the complete verification suite and commit `test: verify LinuxLab Phase 1 learning foundation`.

## Phase 1 acceptance gate

Phase 1 is accepted only when all of these are demonstrated:

1. Wagtail can author Learning Path -> Module -> Lesson structured content.
2. A guest can read published content without an account.
3. Quiz grading is server-side and browser payloads contain no answer key.
4. Attempt stores an exact immutable ActivityVersion.
5. Progress can be rebuilt from Results/events.
6. Publish validates dependencies and freezes a new immutable snapshot.
7. Central builds a signed `.llpkg`; tampering is detected.
8. Node stages, activates, and rolls back packages.
9. Node serves installed learning content with network access blocked.
10. Node event delivery is idempotent under retries.
11. Guest progress is not synchronized until explicitly claimed.
12. The same codebase passes checks under Central and Node profiles.
13. No Architecture Invariant from the approved spec is violated.

## Deferred to later plans

- Browser xterm.js and WebSocket terminal sessions.
- Rootless Podman sandbox execution and validators.
- Formal Exam Engine, autosave/resume, sealed submissions, encrypted exam packages.
- Kiosk/exam-mode desktop controls.
- Python/Git/Database/Network providers.
- Badges/certificates, school/classroom administration, AI tutor.
