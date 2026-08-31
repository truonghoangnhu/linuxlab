# Task 15 Design Amendment — Node Installed-Artifact Learning Execution

Status: Approved design
Date: 2026-08-31
Decision: T15-D27
Parent spec: docs/superpowers/specs/2026-08-31-task15-phase1-release-readiness-gate-design.md

## 1. Purpose

Task 15's Phase 1 Release Gate must exercise a real production activity through the full Central → signed .llpkg → Node → learner execution → Result/Progress → activity.completed → Outbox → Central Inbox path.

The repository already has production-quality assessment providers and immutable installed package/runtime artifacts, but Node learning persistence currently references Central live-authoring models. Specifically:

Attempt.activity_version references activities.ActivityVersion;

Progress.activity_definition references activities.ActivityDefinition;

Node package import creates InstalledActivityVersion, InstalledQuestionVersion, and InstalledQuestionGrading;

Task 13 runtime intentionally reads installed immutable artifacts and does not materialize live authoring rows on Node.

Therefore a package-only activity cannot currently produce a legitimate Node Attempt without bypassing the approved trust boundaries.

This amendment defines the missing Phase 1 execution model.

## 2. Decision T15-D27

Node learning execution references installed immutable artifacts directly. Attempt supports exactly one execution source: Central ActivityVersion or Node InstalledActivityVersion, enforced by database constraint. Node grading and runtime resolve exclusively through the pinned installed artifact/package context; no materialization into authoring tables and no fallback to live authoring state. Progress is keyed by stable activity_definition_public_id across deployments and versions. Existing Central behavior remains backward-compatible.

This is an additive extension to the existing learning model, not a replacement of the Central authoring path.

## 3. Rejected Alternative: Materializing Authoring Rows on Node

Node MUST NOT create local copies of ActivityDefinition, ActivityVersion, Question, or QuestionVersion from installed package artifacts solely to satisfy existing learning foreign keys.

Reasons:

It creates two representations of the same package-delivered content.

It weakens the immutable installed-artifact source of truth established by Tasks 11–13.

It introduces synchronization/materialization semantics unrelated to the Phase 1 package contract.

It risks divergence between installed provenance and reconstructed live-authoring rows.

It makes package rollback/version pinning harder to reason about.

No runtime fallback from installed artifacts to live authoring tables is allowed.

## 4. Data Model

### 4.1 Attempt

Existing Central reference activity_version → activities.ActivityVersion becomes nullable.

Add:

```
installed_activity_version
    → sync.InstalledActivityVersion
    nullable
    on_delete=PROTECT
```

Database invariant:

```
(activity_version IS NOT NULL AND installed_activity_version IS NULL)
OR
(activity_version IS NULL AND installed_activity_version IS NOT NULL)
```

Valid Central attempt:

```
activity_version != NULL
installed_activity_version = NULL
```

Valid Node attempt:

```
activity_version = NULL
installed_activity_version != NULL
```

PROTECT is required so installed provenance cannot be removed while historical attempts depend on it.

### 4.2 Result

Result remains associated with Attempt. No additional installed-artifact FK is required.

### 4.3 Progress

Add:

```
activity_definition_public_id: UUID
```

The existing activity_definition FK becomes nullable.

Central:

```
activity_definition != NULL
activity_definition_public_id = activity_definition.public_id
```

Node:

```
activity_definition = NULL
activity_definition_public_id =
    stable definition UUID carried by InstalledActivityVersion
```

Progress uniqueness must be based on the stable definition identity plus existing learner/context dimensions rather than local authoring PK identity alone.

## 5. Migration and Backward Compatibility

Existing Attempts remain Central-style:

```
activity_version unchanged
installed_activity_version = NULL
```

Existing Progress rows are backfilled:

```
activity_definition_public_id =
    activity_definition.public_id
```

Central behavior remains unchanged. Execution source is determined from the Attempt record, not settings.PROFILE.

## 6. Installed Execution Boundary

Required Node service boundary:

```
start_installed_attempt(...)
```

It must verify:

installed activity belongs to the supplied runtime package context;

package is valid for new execution under active-package rules;

activity version is exact and immutable;

no live ActivityVersion fallback is used.

Created Attempt:

```
installed_activity_version = exact installed version
activity_version = NULL
```

## 7. Package Pinning Semantics

```
Package A active
→ Attempt X starts against A

Package B later becomes active
→ Attempt X remains pinned to A
→ new Attempt Y may use B
```

Submitting an existing Attempt MUST NOT resolve the current active package again.

Historical attempts remain readable if their package becomes archived. Package cleanup/GC must not delete protected installed artifacts.

## 8. Grading Boundary

Node grading resolves only from:

```
Attempt
→ InstalledActivityVersion
→ InstalledPackage / RuntimePackageContext
→ InstalledQuestionVersion
→ InstalledQuestionGrading
```

No answer key or grading-only payload is exposed to the browser. No Node execution path may fetch or reconstruct live authoring question versions.

## 9. Submission Architecture

Use a small source-dispatch layer:

```
submit
→ inspect Attempt execution source

Central source
→ existing authoring execution handler

Installed source
→ installed execution handler
```

Shared responsibilities may include transaction boundary, Result persistence, Progress update, and completion-event generation.

Do not make this profile-based.

## 10. Production Provider Registry

Release E2E uses the real provider chain:

```
AssessmentActivityProvider
→ SingleChoiceProvider
```

Production registry wiring must be explicit application code, not pytest fixture mutation.

Preferred interfaces:

```
build_default_activity_registry()
build_default_question_registry()
```

Requirements:

no FakeProvider;

no ScoredProvider;

no tests-only registry;

no release-gate-only provider;

no hidden fixture-only registration.

The same registry must be used by ordinary runtime/service code and Release E2E.

## 11. HTTP Surface

Minimum Node routes:

```
GET  /learn/<lesson_uuid>/activity/<activity_uuid>/
POST /learn/<lesson_uuid>/activity/<activity_uuid>/submit/
```

Equivalent route structure is acceptable if semantics are preserved.

GET:

```
ACTIVE InstalledPackage
→ RuntimePackageContext
→ InstalledActivityVersion
→ learner-safe provider payload
```

POST:

```
Attempt
→ pinned InstalledActivityVersion
→ pinned package context
→ installed grading
→ Result
→ Progress
→ activity.completed
```

POST must not re-resolve the active package for an existing Attempt.

No browser automation framework is required; real HTTP is sufficient.

## 12. Event Semantics

Reuse:

```
activity.completed
```

It is enqueued in the same transaction as Result/Progress persistence.

T15-D27 final corrective: Task 14 schema v1 remains the sole authority for
this event's wire identity and is unchanged by installed execution. There
is no event schema v2 and no separate aggregate identity for the
installed source. The already-shipped Task 14 v1 contract is:

```
event_type = "activity.completed"
aggregate_type = "attempt"
aggregate_public_id = attempt_public_id
```

identically for both the Central and the Node installed execution source
-- an installed Attempt is still an Attempt, and Task 14's Outbox/Inbox
idempotency and ingestion validation already key on exactly this
`(event_type, aggregate_type, aggregate_public_id)` triple. Widening the
aggregate identity to the activity definition would require an
incompatible Central Inbox/ingestion change this amendment does not make.

`activity_definition_public_id` is not part of the aggregate identity. It
is, and always was (pre-dating T15-D27), one payload field inside the
existing wire envelope (`enqueue._ACTIVITY_COMPLETED_WIRE_FIELDS`), used
by Progress's stable-UUID keying (S4.3) -- carried at the payload level
exactly as before, never promoted to the aggregate.

Do not include local Installed* PKs, answer keys, grading secrets, signing material, or auth secrets.

## 13. Module Ownership

Respect ADR-011:

sync owns installed package lifecycle/artifacts;

learning owns Attempt/Result/Progress;

runtime owns learner-safe installed artifact resolution;

activities / assessment own provider definitions and production registry construction.

Task 15 scripts orchestrate public/domain services and do not reproduce business logic.

## 14. Release E2E Provider Eligibility After T15-D27

Required chain:

```
Central public authoring services
→ ActivityDefinition / ActivityVersion
→ publication snapshot
→ signed .llpkg
→ Node verify/import/stage/activate
→ InstalledActivityVersion
→ production registry
→ real HTTP learner activity route
→ Node installed Attempt
→ installed grading
→ Result
→ Progress(activity_definition_public_id)
→ activity.completed Outbox
→ HttpSyncTransport
→ Central Inbox
→ duplicate/idempotency behavior
```

If any link still requires raw trusted-state ORM/SQL fabrication, implementation must STOP for review.

## 15. Test Strategy

Model invariants

Central Attempt valid.

Node Attempt valid.

both references NULL rejected.

both references non-NULL rejected.

installed artifact referenced by Attempt protected from deletion.

Migration

existing Attempts preserve semantics.

existing Progress rows receive correct stable UUID.

no invalid XOR rows after migration.

Installed execution

active package only for new attempts.

exact installed activity pinned.

installed grading only.

package switch does not change existing Attempt.

no live-authoring fallback.

HTTP

learner-safe GET.

real POST submit.

answer key never exposed.

activation between GET and POST does not change pinned provenance.

invalid context fails closed.

Provider registry

default activity registry contains AssessmentActivityProvider.

default question registry contains SingleChoiceProvider.

registry works outside pytest fixtures.

Release E2E and production runtime use same registry builders.

Integration

```
assessment/single_choice
→ Attempt
→ Result
→ Progress
→ activity.completed
→ Outbox
→ Central Inbox
```

Duplicate delivery remains idempotent.

## 16. Non-Goals

This amendment does NOT:

redesign .llpkg;

introduce DB replication;

materialize Central authoring rows on Node;

add a plugin marketplace;

introduce SPA/browser automation;

redesign Task 14 sync;

create a new activity provider;

add microservices;

alter T15-D01–D26.

## 17. Phase 1 Release-Gate Consequence

Task 15 Tasks 7–10 may resume only after this amendment is implemented and reviewed.

Task 7 must establish:

production registry wiring;

installed execution persistence;

real Node activity GET/submit surface;

public/domain orchestration suitable for the release runner.

Only then may dual-deployment Release E2E proceed.

## 18. Acceptance Criteria

T15-D27 is complete when:

Attempt supports exactly one authoring or installed execution source.

XOR database constraint exists.

Existing Central Attempts remain valid.

Progress has stable activity_definition_public_id.

Existing Progress rows are backfilled.

Node creates Attempts directly against InstalledActivityVersion.

Node grading is installed-artifact only.

No materialization into authoring tables exists.

No live-authoring fallback exists.

Existing Attempts remain pinned across package activation changes.

Production default registries exist outside tests.

AssessmentActivityProvider and SingleChoiceProvider are registered.

Real learner-safe activity GET exists.

Real submit POST exists.

Submit resolves pinned Attempt provenance.

Result and Progress persist correctly.

activity.completed is transactional.

Release E2E can use assessment/single_choice without raw trusted-state ORM/SQL bypass.

T15-D01–D26 remain unchanged.

T15-D27 is the only new Task 15 architectural decision introduced by this amendment.
