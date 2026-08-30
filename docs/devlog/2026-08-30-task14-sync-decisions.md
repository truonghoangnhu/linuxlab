# Task 14 Development Decision Log — Node → Central Event Sync

**Date:** 2026-08-30
**Project:** LinuxLab.vn / LinuxLab Open Lab
**Status:** DESIGN APPROVED — documentation only; implementation has not started
**Predecessor:** Task 13 FINAL PASS at `63a2baa2c33f4d8792befa85c1bb6835e3aa8b60`, 566 tests passing

This file records the architecture choices approved for Task 14. These decisions are normative for the Task 14 spec and future implementation plan. They must not be silently changed during implementation.

## T14-D01 — Direction of event synchronization

Task 14 implements **Node → Central event synchronization only**.

Central → Node remains the existing immutable package deployment path:

`PublishedVersion → signed .llpkg → verify → stage → activate`

Task 14 does not turn `.llpkg` into an event transport, does not add Central → Node event sync, and does not introduce DB replication.

## T14-D02 — Connection initiation

Node initiates outbound synchronization to Central.

Central does not connect inbound to Node. No public inbound Internet port is required on Node.

## T14-D03 — Event idempotency identity

Every sync event has one globally unique immutable `event_id` UUID.

Retries reuse the same `event_id`. Central deduplicates on `event_id`.

## T14-D04 — Delivery guarantee and ACK boundary

Transport is **at-least-once**.

Central ACKs an event only after the normalized `SyncInbox` event has been durably committed, or an exact duplicate has been confirmed.

If Central commits but the ACK is lost, Node retries the same `event_id`; Central returns duplicate success.

The architecture may describe this as **effectively-once ingestion**, but never as exactly-once transport.

## T14-D05 — Ordering

Task 14 provides **no global ordering guarantee**.

Idempotency is the primary invariant. Domain-specific aggregate ordering/versioning may be added later if a concrete domain requires it.

## T14-D06 — Stable sync envelope

The wire contract uses a stable envelope separate from the internal `DomainEvent` persistence model.

Minimum envelope fields:

- `event_id`
- `event_type`
- `occurred_at`
- `source_node_id`
- `aggregate_type`
- `aggregate_public_id`
- `schema_version`
- `payload`

Payload semantics are versioned per event type.

## T14-D07 — Node authentication

Each Node authenticates to Central with a credential unique to that Node over HTTPS Bearer authentication.

Central derives the trusted Node identity from the authenticated credential/registration. `source_node_id` supplied in the event envelope is only a claim that must match the authenticated Node.

Task 11 signing keys are never reused for Node authentication.

## T14-D08 — Bounded batch with per-event ACK

Node sends bounded batches.

Limits apply to both:

- event count;
- total serialized byte size.

The batch ends when either configured limit is reached.

Central returns an independent result for each `event_id`.

## T14-D09 — Permanent rejection and dead letter

Permanent event rejection transitions the Node Outbox event to `DEAD_LETTER`.

Permanent rejection examples include:

- unsupported event type;
- unsupported schema version;
- invalid payload;
- authenticated-node/source-node mismatch;
- idempotency conflict.

Dead-letter events are retained for audit and are not retried automatically.

Transient transport failures do not dead-letter events.

## T14-D10 — Transactional Outbox

A syncable business change, its `DomainEvent`, and its `SyncOutbox` entry must be written in the **same PostgreSQL transaction**.

Network activity is always outside the business transaction.

Rollback of the business transaction rolls back the Outbox entry as well.

## T14-D11 — Outbox stores immutable wire snapshot

`SyncOutbox` stores an immutable snapshot of the wire event envelope at event creation time.

Retries resend the stored snapshot. They do not rebuild payload from current `Attempt`, `Result`, `Progress`, or other domain state.

A link to the internal `DomainEvent` may be retained for trace/audit, but wire semantics do not depend on later reserialization of that model.

## T14-D12 — Durable ingestion separate from domain application

Central durable Inbox ingestion and downstream business processing are separate boundaries.

ACK is based on durable Inbox acceptance, not on downstream processor success.

If processing later fails, Central retains responsibility for retrying/handling that Inbox event internally.

## T14-D13 — Task 14 stops at processor contract

Task 14 creates the durable Inbox and a clear processor contract/boundary.

Task 14 does **not** update Central learning aggregates such as `Attempt`, `Result`, or `Progress`.

Central domain projection/aggregation is deferred.

## T14-D14 — Concurrent worker claim with lease

Multiple Node sync workers may run safely.

Workers claim eligible Outbox events in a short DB transaction using PostgreSQL locking semantics such as `SELECT ... FOR UPDATE SKIP LOCKED`.

Claim transitions events to `IN_FLIGHT` with `claimed_at` and `lease_until`.

No database transaction/lock remains open during the HTTPS request.

Expired leases make events reclaimable.

## T14-D15 — Retry policy

Transient failures use exponential backoff with jitter and a configurable cap.

Transient events are retried indefinitely until success or permanent rejection.

Network timeout, DNS failure, connection refusal, and 5xx are transient.

A long-offline Node must be able to resume synchronization without events being discarded due to retry count.

## T14-D16 — Batch limits are count + bytes

Batch size is bounded by both event count and total serialized bytes.

Concrete thresholds are local configuration, not a hard-coded protocol invariant.

## T14-D17 — Event-level atomic Central ingestion

Central authenticates the request once, but each event is ingested independently.

One poison event must not roll back other valid events in the same request.

## T14-D18 — Central-issued Node identity

Central is the authority that provisions immutable Node UUID identity and credentials.

Hostname, MAC address, and IP address are not trusted Node identities.

## T14-D19 — Opaque bearer credential and hashed Central storage

Node credentials are high-entropy opaque bearer secrets.

Plaintext is held only by Node after issuance/rotation.

Central stores only one-way hashes and verifies secrets through Django's password-hasher framework.

No JWT/OAuth is introduced in Task 14.

## T14-D20 — Explicit syncable event registry

Sync is fail-closed.

Only explicitly registered event types are syncable.

All other `DomainEvent` types are LOCAL-ONLY by default.

The registry is keyed by `(event_type, schema_version)` and defines the serializer/validator contract.

## T14-D21 — One real vertical slice

Task 14 must prove the pipeline with **one real existing domain event** from the repository.

The implementation plan must inspect actual existing `DomainEvent` semantics and choose one appropriate real event. It must not invent an event name that does not exist.

All other events remain LOCAL-ONLY until explicitly approved.

## T14-D22 — Strict event/schema compatibility

Central accepts only registered `(event_type, schema_version)` pairs.

Unknown event type or unsupported version is a permanent rejection.

Central does not guess, best-effort parse, or silently migrate an unknown payload during ingest.

## T14-D23 — Controlled Node provisioning, rotation, and revocation

Task 14 provides minimal controlled administration for:

- issue;
- rotate;
- revoke

Node credentials.

Self-service Node enrollment is out of scope.

## T14-D24 — Node credential storage

Node plaintext credential is stored outside the application database and outside the repository, through local secret file/environment configuration with restricted filesystem permissions.

Credential secrets must not enter migrations, fixtures, application DB dumps, or source control.

## T14-D25 — Dual timestamps

The event preserves Node-provided `occurred_at`.

Central records its own `received_at`.

`received_at` is authoritative for transport ingestion.

`occurred_at` is metadata only and must not drive authentication, idempotency, replay protection, or global ordering.

Clock skew alone must not discard otherwise valid events.

## T14-D26 — Immutable normalized Inbox snapshot

`SyncInbox` stores an immutable normalized snapshot of the authenticated wire event.

Immutable event fields include:

- event_id;
- source node;
- event type;
- schema version;
- occurred_at;
- aggregate identity;
- payload.

Processing metadata such as `processing_state`, `processed_at`, and `processing_error` may change through the processor layer.

## T14-D27 — Same event ID with different content is a conflict

If `event_id` already exists:

- exact same immutable normalized event → `DUPLICATE`, ACK success;
- different immutable content → `IDEMPOTENCY_CONFLICT`.

On conflict:

- original Inbox row remains unchanged;
- event is permanently rejected;
- Node dead-letters it;
- Central records/audits the integrity conflict.

No overwrite, merge, or last-write-wins.

## T14-D28 — Credential rotation grace period

Normal rotation supports a bounded grace period:

- new credential becomes current;
- old credential becomes previous;
- previous is accepted only until `previous_valid_until`.

Emergency revoke disables both immediately.

Credentials are never accepted indefinitely after rotation.

## T14-D29 — HTTPS production policy

Production Node → Central sync requires HTTPS with normal certificate and hostname verification.

No automatic fallback to HTTP exists.

HTTP may be enabled only explicitly for dev/test.

## T14-D30 — Conservative ACK handling

Node marks an event `DELIVERED` only after receiving a valid per-event ACK matching that `event_id`.

Missing, malformed, truncated, or unverifiable ACK results remain retryable.

A generic HTTP 2xx does not imply the entire batch was delivered.

## T14-D31 — Outbox immutability split

`SyncOutbox` consists of:

### Immutable event envelope

- event_id
- event_type
- schema_version
- source_node_id
- occurred_at
- aggregate_type
- aggregate_public_id
- payload

### Controlled mutable delivery metadata

- state
- attempts
- next_attempt_at
- claimed_at
- lease_until
- delivered_at
- rejection_code
- rejection_reason
- created_at

Business code must not modify immutable envelope fields after insert.

## T14-D32 — Explicit enqueue service; no Django signal

The business/application service explicitly calls `enqueue_sync_event()` inside the same transaction that records the domain change and `DomainEvent`.

Task 14 does not use Django `post_save` signals or a background scanner to discover events for Outbox creation.

## T14-D33 — Dedicated Node sync worker

Node synchronization runs in a separate process, conceptually a Django management command such as `sync_events`.

It supports test/admin one-shot execution and can run continuously under a service manager such as systemd.

Task 14 does not introduce a web-process background thread, Celery, Redis, RabbitMQ, or Kafka.

## T14-D34 — Fair deterministic claim scheduling

Eligible events are scheduled by:

1. `next_attempt_at` ascending;
2. oldest created event;
3. deterministic local PK/tie-breaker.

This is worker scheduling only and is not a protocol ordering guarantee.

## T14-D35 — Two-layer HTTP result semantics

HTTP status describes request-level/transport validity.

Per-event result describes event ingestion outcome.

Examples:

- 401/403: credential/Node authorization failure;
- 400: malformed request/batch;
- 413: request exceeds supported batch size;
- 5xx: transient Central failure;
- valid authenticated batch: successful request status with individual `accepted`, `duplicate`, or `rejected` results.

An event-level rejection does not force the whole batch to fail.

## T14-D36 — Auth failure is a channel fault

401/403 is not an event payload defect.

On auth failure:

- affected Outbox events remain retryable;
- they do not become DEAD_LETTER;
- worker enters degraded/pause/long-backoff behavior;
- operator credential/registration recovery is required.

## T14-D37 — Django password hashing for bearer verification

Central identifies `NodeRegistration` by immutable Node ID and uses Django password hash verification for current/previous opaque secrets.

Central never decrypts/reconstructs a plaintext credential.

## T14-D38 — Provisioning prerequisite for sync-enabled Node

A Node may operate fully locally with synchronization disabled.

When sync is disabled:

- local business behavior continues;
- local `DomainEvent` may still exist;
- no `SyncOutbox` is created;
- Task 14 promises no automatic backfill of pre-enrollment events.

When sync is enabled:

- a stable Central-issued `node_id` is mandatory;
- missing/invalid configuration fails loudly instead of silently dropping syncable events.

A temporarily invalid/revoked secret does not stop enqueue; it pauses transport.

## Approved component layout

Task 14 remains inside the existing `linuxlab.sync` Django app. No new Django app is created.

Conceptual responsibility split:

```text
linuxlab/sync/
├── models.py
├── registry.py
├── enqueue.py
├── outbox.py
├── transport.py
├── authentication.py
├── ingestion.py
├── processing.py
├── views.py
├── urls.py
└── management/commands/
    └── sync_events.py
```

The exact filenames may be mechanically adjusted to existing repository structure during implementation planning, but responsibilities must remain separated. See "Repository reality reconciliation" below for the concrete naming/layout this maps to in this repository as of Task 13 (`63a2baa`).

## Approved state machines

### Node Outbox

```text
PENDING
  → IN_FLIGHT
      → DELIVERED
      → DEAD_LETTER
      → PENDING        # transient retry / expired lease recovery
```

### Central Inbox processing

```text
RECEIVED
  → PROCESSED
  → FAILED
```

Inbox processing state is not part of Node ACK success.

## Error taxonomy

### Channel/auth failure

401/403 → preserve events, pause/degrade channel, operator recovery.

### Transient transport failure

Timeout/DNS/connect/5xx/missing ACK → retry with exponential backoff + jitter.

### Request protocol failure

Malformed/oversized request → request-level failure; do not infer unconfirmed delivery.

### Permanent event failure

Unsupported type/version, invalid payload, source-node mismatch, idempotency conflict → DEAD_LETTER.

### Central processing failure

Inbox already durable → no Node retry; Central retries/handles processing internally.

## Retention

Task 14 does not automatically hard-delete delivered Outbox rows, dead letters, or Inbox rows.

Automatic GC/retention scheduling is deferred.

## Required verification themes

Task 14 tests must cover at least:

- business transaction rollback leaves no Outbox;
- sync-disabled local operation creates no Outbox;
- sync-enabled missing Node ID fails loudly;
- immutable Outbox envelope;
- fair multi-worker claim with short lease;
- expired lease reclaim;
- retry backoff/jitter behavior;
- bounded batch count + bytes;
- per-event ACK parsing;
- missing/malformed ACK preserves retry;
- 401/403 preserves retryable events;
- strict registry `(event_type, schema_version)`;
- bearer current/previous/grace/revocation behavior;
- source Node spoof rejection;
- exact duplicate success;
- same-ID/different-content conflict;
- one poison event does not roll back good events;
- Central durable Inbox before ACK;
- processor failure does not cause Node retransmission;
- one real DomainEvent vertical slice end-to-end;
- no Central/Wagtail/package fallback;
- no Task 11/12 regression;
- no Task 14 Central → Node package transport.

## Non-goals

Task 14 does not implement:

- Central → Node event sync;
- automatic `.llpkg` download/deployment;
- Central `Attempt`/`Result`/`Progress` projection;
- self-service Node enrollment;
- dead-letter repair UI;
- result reconciliation UI;
- Celery;
- Redis;
- RabbitMQ;
- Kafka;
- global event ordering;
- automatic Inbox/Outbox GC;
- DB replication.

---

## Repository reality reconciliation (2026-08-30)

Added during the docs-only write-up (this file's content above is otherwise verbatim from the approved design). Semantics of T14-D01 through T14-D38 are unchanged; this section only maps the conceptual layout above onto what the checked-in authoritative Task 14 prompt (`docs/codex/task-prompts/TASK_14_idempotent-outbox-inbox-event-sync.md`) and current repository state actually require or already contain.

**Authoritative naming.** The checked-in Task 14 prompt names the exact deliverables (its own "Produces:" line), which take precedence over this design's more conceptual `SyncOutbox`/`SyncInbox` naming, the same way Task 12's "Produces:" line was authoritative for `InstalledPackage`/`import_package()`/etc.:

| This design's conceptual name | Checked-in prompt's authoritative name |
|---|---|
| `SyncOutbox` | `OutboxMessage` |
| `SyncInbox` | `InboxMessage` |
| transport abstraction | `SyncTransport` (with `HttpSyncTransport` and `FileSyncTransport` implementations) |
| worker flush operation | `flush_outbox()` |
| ingestion endpoint | `/api/v1/sync/events/` |

None of T14-D01–D38's *semantics* change under this renaming — every decision above still holds verbatim under the authoritative names.

**File-based transport is required, not optional.** The checked-in prompt explicitly requires "`FileSyncTransport`... using exactly the same event envelope for offline/USB-style integration tests," alongside `HttpSyncTransport`. This was not spelled out in section 13 of the design spec (which only discussed HTTPS) but does not conflict with it: ADR-010 itself states "Transport is independent from the package/event format so HTTPS, LAN, file, or later USB workflows can use the same contracts" — a file-based transport implementing the same `SyncTransport` interface is exactly what ADR-010 already anticipates. T14-D29's "HTTPS production policy" governs *production* Node→Central sync specifically; it does not forbid a deterministic file-based transport for offline/USB/test scenarios. The design spec (written alongside this file) makes `transport.py` an explicit `SyncTransport` interface with both implementations.

**Existing `linuxlab/sync/` layout.** As of Task 13 (`63a2baa`), `linuxlab/sync/` already contains, from Tasks 12–13:

- `models.py` — Task 12's `InstalledPackage`/`InstalledPublication`/`InstalledActivityVersion`/`InstalledQuestionVersion`/`InstalledQuestionGrading`;
- `services.py` — Task 12's `import_package()`/`activate_package()`/`rollback_package()`/`get_active_package()`;
- `views.py` / `urls.py` — Task 13's `LearnView` and the `/learn/<uuid:source_public_id>/` route;
- `tests/` — an established one-file-per-concern convention (`test_models.py`, `test_import_package.py`, `test_activation.py`, `test_rollback.py`, `test_integration.py`, `test_views.py`, `test_offline.py`, plus a shared `conftest.py`/`_wagtail_bootstrap.py`).

Task 14's new models (`OutboxMessage`, `InboxMessage`, `NodeRegistration`) must coexist in `models.py` (or a models package split, decided at implementation-planning time) alongside Task 12's models without modifying them; Task 14's new views/URLs must coexist with Task 13's `LearnView`/route without modifying them; Task 14 tests should follow the existing one-file-per-concern convention (e.g. `test_outbox.py`, `test_ingestion.py`, `test_authentication.py`, `test_transport.py`) rather than one large file.

**Existing `DomainEvent` production paths.** `linuxlab.core.models.DomainEvent` (fields: `event_id`, `event_type`, `aggregate_id`, `payload`, `created_at`, `processed_at`) and `linuxlab.core.events.publish_event(event_type, payload, aggregate_id)` already exist and are already used in production. As of this writing, the **only** event type actually published outside tests is `"activity.completed"`, from exactly two call sites in `linuxlab/learning/services.py` (`claim_activity_completion()` and `submit_attempt()`), both immediately after `Result.objects.create(...)` in the same transaction — this is a strong, obvious candidate for T14-D21's "one real vertical slice," matching the checked-in prompt's own phrasing ("same transaction as authoritative local Result/domain event") closely. This is recorded as a candidate only, per the docs-only instructions; the implementation plan must confirm and select it (or another) explicitly rather than this document deciding it.

No T14-D39 was added for any of the above — these are naming/layout reconciliations, not new architectural decisions.
