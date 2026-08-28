# LinuxLab Architecture Specification v1

**Status:** Approved design baseline  
**Date:** 2026-08-28  
**Scope:** LinuxLab self-learning platform, Open Lab node, browser-based Linux practice, offline operation, and extensible computer-based assessment

## 1. Product vision

LinuxLab is an open learning and practice platform for Linux and open-source technology, designed first for self-study and school computer labs, while preserving a clean path toward stronger LMS and assessment capabilities.

LinuxLab is not designed as a traditional school-management LMS. Its primary value is:

- structured Vietnamese learning content for Linux and open-source tools;
- interactive practice, especially real Linux terminal activities;
- offline-first deployment in school computer labs;
- reusable assessment and exam capabilities;
- modular expansion without rewriting the core;
- long-term maintainability by a small team.

The guiding product principle is **guest-first self-learning with optional identity, local-first lab operation, and modular capability growth**.

## 2. Architectural goals

LinuxLab v1 must:

1. Run as one maintainable Django/Wagtail codebase.
2. Support two main deployment profiles: Central and Node.
3. Allow a Node to continue learning, lab practice, and exams when Internet connectivity is unavailable.
4. Keep content authoring separate from learning state and execution state.
5. Allow new activity types to be added through stable extension contracts.
6. Reuse one assessment model across self-study, practice, quizzes, and exams.
7. Preserve published versions so historical attempts and exam results remain reproducible.
8. Avoid direct database replication between Central and Node.
9. Keep user management lightweight while preserving a path to classrooms, schools, and formal exams.
10. Make security boundaries explicit, especially around browser terminals and offline exams.

## 3. Non-goals for v1

The following are intentionally not part of the v1 core:

- full school information system functions;
- complex class rosters and academic-year administration;
- gradebook workflows comparable to Moodle/Open edX;
- messaging and social networking;
- payments;
- SCORM/LTI compatibility;
- AI tutoring as a core dependency;
- Kubernetes or microservice infrastructure;
- high-stakes remote proctoring;
- automatic reliance on Internet connectivity.

These may be added later as modules when there is a real use case.

## 4. Core architectural style

LinuxLab uses a **modular monolith**.

The first implementation is one Django project with clearly bounded Django apps and one primary PostgreSQL database per deployment instance. Modules expose public service interfaces and domain events. Modules must not mutate another module's owned models directly.

This architecture deliberately avoids early microservices while defining boundaries that permit extraction later if scale or isolation requires it.

### 4.1 Baseline application stack

- Python / Django
- Wagtail for structured authoring and editorial workflows
- PostgreSQL as the source-of-truth database for both Central and Node
- Server-rendered Django templates for the main UI
- HTMX and Alpine.js for progressive interaction where needed
- xterm.js for the browser terminal UI
- a sandbox-provider abstraction, with rootless Podman as the initial server-side implementation
- background workers using a transactional outbox pattern before introducing a message broker

React/Vue, Kafka/RabbitMQ, Kubernetes, and independent microservices are not baseline requirements.

## 5. Deployment architecture

### 5.1 LinuxLab Central

Central is the Internet-facing authoritative platform.

Responsibilities:

- Wagtail content authoring;
- publishing and immutable version creation;
- package registry;
- account management;
- aggregated progress and analytics;
- Node registration and capability visibility;
- exam authoring and assignment;
- package distribution;
- receipt of Node events and results.

### 5.2 LinuxLab Node

Node is a local deployment for a school, workshop, or computer lab.

Responsibilities:

- serve published learning content over LAN;
- operate without Internet after required packages are installed;
- run local learning attempts and progress;
- host local browser-terminal sandboxes when enabled;
- conduct local exams;
- autosave local responses and results;
- stage, activate, and roll back content packages;
- synchronize events and results when a transport becomes available.

A Node may run in standalone mode and is not required to maintain a persistent Central connection.

### 5.3 One codebase, multiple profiles

Central and Node use the same codebase with deployment-profile configuration. Features are enabled or disabled by profile and capability, not by maintaining separate forks.

## 6. Domain boundaries

The initial codebase is organized conceptually as:

```text
linuxlab/
├── core/          shared platform primitives
├── identity/      account, learner, context, authorization
├── content/       Wagtail learning content and publication
├── learning/      attempts, results, progress, completion
├── activities/    activity contracts and registry
├── assessment/    questions, versions, scoring, pools, blueprints
├── quiz/          assessment activity provider
├── labs/          terminal, sandbox, lab profile, validators
├── catalog/       software, distributions, install recipes, resources
├── sync/          node, package, inbox/outbox, transports
├── exam/          exam orchestration and audit
└── publication/   dependency validation, immutable versions, package building
```

Each module owns its data. Cross-module state changes occur through a public service contract or domain event.

## 7. Core domain model v1

### 7.1 Content domain

- `LearningPath`
- `Module`
- `Lesson`
- `ContentBlock`
- immutable published versions of each relevant learning entity

Wagtail manages editorial revisions and structured content. Learning state never lives in Wagtail page records.

### 7.2 Learning domain

- `ActivityDefinition`
- `ActivityVersion`
- `Attempt`
- `Result`
- `Progress`
- `CompletionPolicy`

`Attempt` is historical execution data. `Progress` is derived state and can be rebuilt from authoritative activity results/events.

### 7.3 Identity domain

- `Account`
- `Learner`
- `AnonymousLearner`
- `LearningContext`
- `RoleAssignment`
- `ServicePrincipal`
- `NodeIdentity`

Account and Learner are intentionally separate concepts.

### 7.4 Lab domain

- `LabProfile`
- `LabProfileVersion`
- `LabSession`
- `Validator`
- `LabResult`
- `SandboxProvider`

Lessons and activities reference lab profiles by stable identity/version, never by Docker/Podman command details.

### 7.5 Catalog domain

- `Software`
- `Distribution`
- `InstallationRecipe`
- `Resource`

Content references catalog entities instead of duplicating software metadata and installation instructions across lessons.

## 8. Stable identity and versioning

Every cross-instance entity uses a UUID as its stable identity. Local database primary keys are implementation details and must never be used as Central–Node identities.

Published learning entities use immutable versions. Attempts and exam records refer to the exact version used at the time of activity.

Wagtail editorial revisions and LinuxLab published versions are different concepts:

```text
Wagtail draft/revision
        ↓ review
LinuxLab publish validation
        ↓
Immutable LinuxLab PublishedVersion
```

Only published versions may be included in learning packages, synchronized to Nodes, or referenced by formal exam versions.

## 9. Activity Engine

All interactive learning is modeled as an Activity.

The Activity contract exposes common lifecycle concepts:

- prepare;
- start;
- save;
- submit;
- evaluate;
- complete.

Not every provider must implement these identically, but the Learning Engine communicates through the contract rather than provider internals.

Initial or expected providers include:

- Assessment/Quiz;
- Terminal;
- Python;
- Git;
- Database;
- Network.

Future activity types register through an Activity Provider registry without modifying the Learning core.

### 9.1 Capability requirements

Activity versions may declare requirements such as:

- shell;
- filesystem;
- python3;
- network;
- database;
- sandbox profile.

A Node advertises capabilities. Compatibility is checked before activation or exam assignment.

## 10. Browser Linux lab architecture

xterm.js is only the browser terminal UI. It never directly provides Linux execution.

The normal server-backed path is:

```text
Browser/xterm.js
      │ WebSocket
      ▼
Sandbox gateway/service
      │
SandboxProvider
      │
Disposable Linux environment
```

The first provider is intended to use rootless Podman with strict resource and privilege controls.

Minimum sandbox defaults:

- non-root learner;
- no privileged mode;
- no host filesystem mounts;
- no container runtime socket exposure;
- no device access;
- CPU/RAM/PID/disk limits;
- short TTL;
- network disabled unless the activity explicitly needs it.

Browser/WASM Linux may later be implemented as another provider for quick demos or offline experiments, but lesson content must not depend on a particular execution technology.

### 10.1 Validators

Lab success is evaluated through versioned validator contracts, for example:

- file exists;
- directory exists;
- expected command output;
- file content;
- process running;
- port listening;
- Python test;
- custom validator.

Validators are reusable resources, testable in preview mode, and are not hard-coded into lesson content.

## 11. Guest-first identity and authorization

LinuxLab allows immediate self-study without an account.

### 11.1 Anonymous learner

Anonymous progress remains local by default. Anonymous learning events are not automatically synchronized to Central.

If a learner later creates or uses an account, the platform may explicitly claim/merge local progress into a registered Learner identity.

### 11.2 Registered learner

A registered account is used only when the user needs durable cross-device or cross-Node state, bookmarks, badges, certificates, or formal participation.

### 11.3 Contextual roles

LinuxLab does not use fixed `TeacherUser` or `StudentUser` classes. Roles are assignments scoped to a context/resource.

Initial roles:

- Learner;
- ContentEditor;
- Instructor;
- ExamInvigilator;
- NodeAdministrator;
- SystemAdministrator.

Authorization checks are made through a policy service equivalent to:

```text
can(actor, action, resource, context) -> ALLOW | DENY
```

### 11.4 Node and service identities

Nodes and background services have their own principals and least-privilege permissions. They never impersonate a system administrator account.

Central passwords are not copied to Nodes. Offline credentials are limited by Node, context, and/or time period.

## 12. Central–Node synchronization

Central and Node never replicate application databases directly.

LinuxLab uses two synchronization primitives:

1. immutable versioned packages flowing primarily Central → Node;
2. idempotent events/results flowing primarily Node → Central.

### 12.1 Package model

A logical LinuxLab package (`.llpkg`) contains a manifest and published artifacts.

The manifest contains at minimum:

- package UUID;
- package type;
- semantic/version identifier;
- creation timestamp;
- content hashes;
- dependencies;
- minimum compatible LinuxLab version;
- cryptographic signature metadata.

Packages are immutable. Any change produces a new version.

Large assets are content-addressed so unchanged media or runtime artifacts do not need to be transferred repeatedly.

### 12.2 Node package lifecycle

```text
Downloaded → Verified → Staged → Active → Archived
```

Activation is an explicit local operation or approved policy action. A Node can keep a current active version while a newer version is staged. Rollback selects a previously verified immutable version.

### 12.3 Event model

Node events include globally unique event IDs and are processed idempotently.

Examples:

- activity_started;
- answer_saved;
- activity_submitted;
- activity_completed;
- lesson_completed;
- lab_validated;
- exam_started;
- exam_submitted.

Node writes authoritative local state and an outbox entry in the same transaction. A sync worker later transports events. Central acknowledges processed event IDs. Retries never create duplicate effects.

### 12.4 Data ownership conflict rule

Default authoritative ownership is:

- learning content: Central;
- activity definitions: Central;
- lab profiles: Central;
- exam definitions: Central;
- local Node configuration: Node;
- attempts: instance where created;
- exam submission: Node/session where created;
- progress: derived from authoritative events/results.

The architecture avoids general-purpose two-way record merging.

### 12.5 Sync transport abstraction

Packages/events are transport-independent. Supported or planned transports include:

- HTTPS;
- LAN;
- local file;
- USB/offline transfer.

The same artifact format is used regardless of transport.

## 13. Assessment architecture

LinuxLab uses a shared Question Bank across self-study, practice, quiz, diagnostic tests, and exams.

Assessment owns:

- `Question`;
- `QuestionVersion`;
- `QuestionType` provider;
- choices and prompt data;
- answer key/secret data;
- `ScoringPolicyVersion`;
- `QuestionPool`;
- `Blueprint`.

The Assessment Engine owns question rendering contracts, answer validation, and grading. Exam does not duplicate this logic.

Initial question providers may include:

- single choice;
- multiple choice;
- true/false;
- four-statement true/false (TF4);
- short answer;
- numeric answer.

Programming and terminal tasks remain Activity providers rather than being forced into the question-type model.

### 13.1 Answer secrecy

Correct answers and scoring secrets are never sent to the learner browser merely to be hidden with JavaScript. Grading occurs on Central/Node server-side services.

## 14. Exam Engine

The Exam Engine orchestrates formal assessment and reuses Activity and Assessment engines.

Exam-owned entities include:

- `Exam`;
- `ExamVersion`;
- `ExamSection`;
- `ExamSession`;
- `ExamCandidate`;
- `ExamAttempt`;
- `ExamResponse`;
- `ExamSubmission`;
- `ExamEvent`.

An ExamVersion is immutable after freeze/publish.

### 14.1 Reproducible randomization

Each ExamAttempt records enough data to exactly reproduce the allocated variant:

- random seed;
- selected question/activity versions;
- question order;
- choice order;
- scoring-policy versions.

Reconnect or machine replacement resumes the same ExamAttempt and does not generate a new variant.

### 14.2 Autosave and finalization

Responses are mutable and autosaved during an active attempt. Final submission creates a sealed immutable submission. Administrative changes after sealing are recorded as explicit audit events, never silent record edits.

### 14.3 Offline exam

An Exam Package can be assigned and preloaded onto a Node, verified and kept locked before an exam session. The Node can conduct the session and grade supported activities without Internet access.

Result synchronization occurs after connectivity or offline transport becomes available.

### 14.4 Exam-mode boundary

LinuxLab web software alone does not guarantee kiosk-level exam security. Restricting Alt+Tab, USB, external applications, network destinations, and the desktop environment belongs to the Open Lab deployment/exam-mode profile. Classroom monitoring tools such as Veyon remain external cooperating systems rather than becoming dependencies of the LinuxLab Exam Engine.

## 15. Authoring and publishing

Wagtail provides structured authoring for Learning Paths, Modules, Lessons, articles, documentation, and reusable content blocks.

Typical blocks include:

- headings and rich text;
- image/video;
- command;
- expected output;
- tip/warning;
- download;
- activity reference;
- assessment reference.

Activity definitions, Question Bank items, Lab Profiles, and validators are managed as reusable domain resources, not duplicated inside Lesson page content.

### 15.1 Editorial workflow

```text
Draft → Preview/Test → Review → Validation → Publish/Freeze
```

In a small deployment, one person may hold Author, Reviewer, and Publisher permissions while the workflow still exists structurally.

### 15.2 Publication validation

Publishing validates:

- content references;
- activity references;
- question versions;
- answer/scoring consistency;
- lab-profile dependencies;
- validator availability;
- required assets;
- minimum platform compatibility;
- licensing/attribution metadata when applicable.

Failed dependency validation prevents publication.

### 15.3 Snapshot semantics

A PublishedVersion records exact dependency versions. It never means “latest”. A later update creates a new draft and, after approval, a new PublishedVersion.

### 15.4 Package building

Packages are built only from published immutable artifacts. Node never consumes Wagtail drafts or live editorial database state.

## 16. Content licensing and attribution

Content/resource metadata includes license and attribution information. LinuxLab should support common open-content licensing declarations and distinguish external copyrighted resources.

Export/package operations preserve attribution metadata. This is essential for LinuxLab's open-education goals and future community contribution workflows.

## 17. Internal and external APIs

### 17.1 Internal API

Modules communicate in-process through explicit Python service interfaces and domain events. Direct cross-module model mutations are prohibited.

### 17.2 External API

REST/JSON is the default external API style for Node synchronization, future mobile/frontends, and integrations.

External APIs are versioned, for example:

```text
/api/v1/learning/
/api/v1/catalog/
/api/v1/sync/
/api/v1/exams/
```

No GraphQL dependency is required for v1.

## 18. Domain events and background work

Operations that require immediate consistency use service calls within the local application boundary.

Secondary reactions use domain events, for example:

```text
activity.completed
   ├── progress projection
   ├── sync outbox
   ├── analytics
   └── future badge/certificate subscribers
```

The initial event mechanism uses PostgreSQL-backed transactional outbox/inbox records and background workers. A dedicated message broker is introduced only if demonstrated load or isolation requirements justify it.

## 19. Security model

LinuxLab adopts least privilege and explicit trust boundaries.

Key requirements:

- sandbox execution is isolated from the Django application process;
- no user command is executed directly by the web process through unrestricted shell subprocesses;
- browser terminals receive only session capabilities/tokens, not host credentials;
- answer keys are server-side secrets;
- packages are hash-verified and signed;
- Node identities use cryptographic credentials separate from human accounts;
- services have dedicated principals;
- offline credentials are scope/time limited;
- audit events are append-oriented for exam operations;
- root/host-administrator compromise is outside the guarantee of an offline exam package and must be documented as part of the threat model.

## 20. Privacy model

LinuxLab follows data minimization:

- guest learning requires no personal profile;
- anonymous progress remains local by default;
- Node receives only identity fields needed for its local context;
- school/class/date-of-birth/student identifiers are not core Account fields unless a later module requires them;
- formal exam identity is modeled through ExamCandidate and assurance policy rather than expanding all learner profiles.

## 21. Failure handling

### 21.1 Internet loss

Node learning, local labs, and active exams continue using installed packages and local data.

### 21.2 Central unavailability

Node queues events/results in its outbox until a transport becomes available.

### 21.3 Node update failure

New packages remain staged until verification and activation succeed. The previous active package remains available for rollback.

### 21.4 Learner machine failure during exam

Autosaved responses remain on Node. The candidate may resume the same ExamAttempt on an authorized replacement client.

### 21.5 Duplicate synchronization

Globally unique event IDs and idempotent inbox processing prevent duplicate side effects.

## 22. Testing strategy

LinuxLab requires tests at several boundaries.

### 22.1 Unit tests

- domain policies;
- scoring rules;
- completion policies;
- validators;
- package manifests;
- authorization policies.

### 22.2 Contract tests

Each provider implementation must pass stable contract tests for:

- Activity Provider;
- Question Type Provider;
- Sandbox Provider;
- Validator;
- Export/Package Provider;
- Sync Transport.

### 22.3 Integration tests

- content publish → package → Node import;
- Node attempt → outbox → Central inbox;
- offline exam → sealed submission → later sync;
- sandbox start/evaluate/cleanup;
- guest progress claim to registered Learner.

### 22.4 Failure-mode tests

- Internet loss during learning/exam;
- repeated event delivery;
- stale Node version;
- incompatible package dependencies;
- package tampering/signature failure;
- client reconnection during exam;
- sandbox timeout/resource exhaustion.

## 23. Upgrade and compatibility policy

Packages declare minimum LinuxLab platform compatibility. A Node rejects packages requiring a newer incompatible runtime.

Database migrations are local deployment concerns; Central and Nodes are not required to upgrade simultaneously. External API and package format compatibility must therefore be explicit and versioned.

## 24. Extension points

The core intentionally exposes a small number of extension contracts:

- Activity Provider;
- Question Type Provider;
- Validator;
- Sandbox Provider;
- Authentication/identity assurance provider;
- Achievement provider;
- Export/Package Provider;
- Sync Transport.

New capabilities should be introduced through one of these boundaries or through a new bounded module with a public service contract. The project must not create a general-purpose plugin framework unless real deployment needs justify it.

## 25. Phased capability roadmap

### Phase 1 — LinuxLab learning foundation

- Django/Wagtail Central;
- structured Learning Path/Module/Lesson authoring;
- command, tip, media, and activity-reference blocks;
- guest-first learning;
- lightweight quiz/assessment;
- progress;
- catalog/docs;
- immutable publication/version model;
- Node package import and local serving.

### Phase 1.5 — Interactive lab

- xterm.js;
- sandbox service;
- rootless Podman provider;
- Linux Basics lab profile;
- reusable validators;
- automatic lab completion checks;
- local Node sandbox operation.

### Phase 2 — Assessment and offline exam

- shared Question Bank;
- exam versions/sessions/candidates;
- randomization and autosave;
- sealed submissions;
- signed/encrypted Node exam packages;
- offline exam operation and result synchronization;
- Open Lab kiosk/exam-mode deployment profile.

### Phase 3 — Advanced labs and ecosystem

- Python/Git/Database/Network activity providers;
- multi-container labs where required;
- richer analytics;
- optional badges/certificates;
- school/classroom modules if demand exists;
- optional browser/WASM Linux quick-try provider.

## 26. Architecture decision record index

### ADR-001 — Django + Wagtail as the Platform Foundation

Django is the application framework; Wagtail is the structured authoring/editorial layer inside the same Django platform.

### ADR-002 — Modular Monolith First

LinuxLab starts as one modular Django monolith with bounded apps and explicit service/event boundaries; microservices require measured justification.

### ADR-003 — Separate Content, Learning State, and Execution State

Authored content, learner state, and runtime/sandbox state are separate domains and must not be collapsed into Wagtail page models.

### ADR-004 — Activity as the Learning Extension Boundary

Interactive capabilities enter through versioned Activity references. This high-level decision is refined by ADR-009.

### ADR-005 — Dedicated Sandbox Boundary for Command Execution

Learner-controlled commands never execute inside the Django web process; interactive command execution lives behind a replaceable sandbox-provider boundary.

### ADR-006 — Guest-First Learning

Self-study begins without mandatory registration; anonymous progress is local by default. This product decision is refined by ADR-012.

### ADR-007 — PostgreSQL as the Source-of-Truth Database per Deployment

Central and every Node use independent PostgreSQL databases. Cross-instance synchronization is application-level, not database replication.

### ADR-008 — Central–Node Offline Architecture

One Django/Wagtail codebase; Central and Node are deployment profiles. Node can learn, practice, and conduct exams offline.

### ADR-009 — Activity Contract Architecture

All interactive learning is represented as versioned Activities. Learning communicates through stable provider contracts.

### ADR-010 — Package/Event Synchronization

Central and Node do not replicate databases. Immutable packages move published definitions downward; idempotent events/results move activity state upward. Transport is independent.

### ADR-011 — Module Data Ownership & Service Boundaries

Each module owns its models. Cross-module changes use public service contracts/domain events. UUID + immutable version is the cross-instance identity.

### ADR-012 — Guest-first Identity & Contextual Authorization

Learning can begin without an account. Account, Learner, and ExamCandidate are distinct. Authorization is role + scope/context. Node/service identities are independent principals.

### ADR-013 — Shared Assessment & Versioned Exam Architecture

Question Bank and scoring models are shared by self-study, quiz, practice, and exam. Exam orchestrates sessions and integrity while reusing Assessment and Activity engines.

### ADR-014 — Structured Authoring & Immutable Publishing

Wagtail manages structured drafts and review. LinuxLab publication validates dependencies and freezes immutable versions. Nodes consume only signed published packages with staging and rollback.

## 27. Architecture invariants

The following are treated as non-negotiable design invariants unless replaced by a later explicit ADR:

1. No Central–Node database replication.
2. No direct cross-module mutation of another module's owned data.
3. No learning attempt points to an unversioned mutable definition.
4. No learner browser receives answer keys as hidden client data.
5. No lesson embeds container-runtime implementation details.
6. No Node requires continuous Internet for installed learning/exam content.
7. No public content package is built from an editorial draft.
8. No exam final submission is silently mutable after sealing.
9. No human password database is copied from Central to Node.
10. No microservice is introduced without a measured reason that outweighs modular-monolith simplicity.

## 28. Success criteria for the first durable release

LinuxLab architecture v1 is considered validated when one installation can demonstrate all of the following without special-case code paths:

1. An author creates and publishes a Linux learning path in Wagtail.
2. A learner can study it anonymously on Central.
3. The same published version can be packaged and activated on a Node.
4. The Node remains usable after Internet is disconnected.
5. A learner can complete an Activity and produce local progress.
6. A terminal lab can be evaluated through a validator without exposing host privileges.
7. Local events can later synchronize idempotently to Central.
8. A versioned assessment can be reused in self-study and an ExamVersion.
9. An exam can autosave, finalize, seal, and later synchronize from a Node.
10. Updating content creates a new version without invalidating historical attempts or exam auditability.

