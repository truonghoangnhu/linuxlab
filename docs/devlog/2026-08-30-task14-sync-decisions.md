# Task 14 Development Decision Log — Node → Central Event Sync

**Date:** 2026-08-30
**Project:** LinuxLab.vn / LinuxLab Open Lab
**Status:** CORRECTIVE APPLIED — independent review returned verdict B (TASK 14 CONDITIONAL); all eight Important findings corrected, see "Corrective implementation record" at the end of this file; awaiting independent focused corrective re-review
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

## T14-D39 — FileSyncTransport is non-production reference/test transport in Task 14

`HttpSyncTransport` is the production Node → Central transport for Task 14. Production delivery semantics remain exactly:

```text
Node Outbox
  → HTTPS push
  → Central authenticated ingestion
  → durable SyncInbox commit / exact duplicate
  → valid per-event ACK
  → DELIVERED
```

`FileSyncTransport` exists because the authoritative Task 14 prompt requires the transport abstraction and a file implementation. In Task 14, `FileSyncTransport` is limited to:

- development;
- tests;
- reference implementation of the transport abstraction;
- deterministic fixtures / local export inspection.

Task 14 does **not** claim that `FileSyncTransport` implements a complete production USB/air-gap synchronization workflow.

A successful local file write alone **must not** be treated as proof that Central durably accepted the event in a production workflow.

Task 14 does not design or implement the additional production air-gap protocol needed for:

- file authenticity/integrity trust;
- Central import authorization;
- operator transfer workflow;
- durable Inbox acceptance;
- ACK/result round-trip back to Node;
- reconciliation after delayed import.

ADR-010 remains compatible because it permits file/later USB workflows as an architectural possibility, but Task 14 does not complete that production workflow.

This decision does not change:

- T14-D01 (Node → Central direction);
- T14-D04 (durable Inbox ACK boundary);
- T14-D08 (per-event acknowledgement);
- T14-D29 (production HTTPS policy);
- T14-D30 (conservative ACK handling).

No automatic state transition to `DELIVERED` may be justified solely by a `FileSyncTransport` write in production semantics.

## T14-D40 — Deterministic HTTP 413 recovery

- 413 for a batch containing multiple events is a batch-sizing fault, not a
  permanent rejection of every event.
- The Node must split/adapt that batch and retry smaller batches.
- If the batch has been reduced to one event and Central still returns 413,
  that event is permanently unsendable under the current transport contract.
- That singleton event transitions to DEAD_LETTER with code EVENT_TOO_LARGE.
- Good events must continue and must not be starved behind the oversized event.
- No new Outbox state is introduced.
- HTTP 400 is not automatically EVENT_TOO_LARGE; deterministic 400 must enter
  an operator-visible/degraded non-tight-loop path rather than replay the same
  malformed request indefinitely.

This decision is a corrective addition approved 2026-08-31, following the independent Task 14 architecture/security review (verdict B, TASK 14 CONDITIONAL). It does not change T14-D01 through T14-D39; it resolves an underspecified gap in T14-D16 (batch limits are count + bytes) and T14-D09 (permanent rejection and dead letter) by defining exactly what happens when the byte limit Task 14 itself enforces locally is still rejected by Central as oversized.

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
- DB replication;
- production air-gap/USB synchronization workflow (file authenticity/integrity trust, Central import authorization, operator transfer workflow, ACK/result round-trip, reconciliation after delayed import — see T14-D39).

---

## Repository reality reconciliation (2026-08-30)

Added during the docs-only write-up (this file's content above is otherwise verbatim from the approved design). Semantics of T14-D01 through T14-D38 are unchanged; this section only maps the conceptual layout above onto what the checked-in authoritative Task 14 prompt (`docs/codex/task-prompts/TASK_14_idempotent-outbox-inbox-event-sync.md`) and current repository state actually require or already contain. *(Corrective, same day: an independent review of this section's original wording found it did not clearly rule out treating a `FileSyncTransport` write as delivery proof. That gap is closed by T14-D39, added above; this section's file-transport paragraph below was reworded to match — no other paragraph in this section changed.)*

**Authoritative naming.** The checked-in Task 14 prompt names the exact deliverables (its own "Produces:" line), which take precedence over this design's more conceptual `SyncOutbox`/`SyncInbox` naming, the same way Task 12's "Produces:" line was authoritative for `InstalledPackage`/`import_package()`/etc.:

| This design's conceptual name | Checked-in prompt's authoritative name |
|---|---|
| `SyncOutbox` | `OutboxMessage` |
| `SyncInbox` | `InboxMessage` |
| transport abstraction | `SyncTransport` (with `HttpSyncTransport` and `FileSyncTransport` implementations) |
| worker flush operation | `flush_outbox()` |
| ingestion endpoint | `/api/v1/sync/events/` |

None of T14-D01–D38's *semantics* change under this renaming — every decision above still holds verbatim under the authoritative names.

**File-based transport is required, not optional — but is dev/test/reference scope only.** The checked-in prompt explicitly requires "`FileSyncTransport`... using exactly the same event envelope for offline/USB-style integration tests," alongside `HttpSyncTransport`. This does not conflict with T14-D29's HTTPS-only production policy: ADR-010 states "Transport is independent from the package/event format so HTTPS, LAN, file, or later USB workflows can use the same contracts," anticipating a file-based transport implementing the same `SyncTransport` interface. However, an independent review of the first version of these documents flagged a dangerous ambiguity: successfully writing an event batch to a file is **not** equivalent to Central durably ingesting that event into `SyncInbox`, and the original wording here did not say so explicitly. **T14-D39** (below) is the human-approved correction: `HttpSyncTransport` is the sole production Node → Central transport in Task 14; `FileSyncTransport` is limited to development, tests, and reference/fixture use, and a local file write alone must never be treated as `DELIVERED`. The design spec's transport section reflects this distinction explicitly.

**Existing `linuxlab/sync/` layout.** As of Task 13 (`63a2baa`), `linuxlab/sync/` already contains, from Tasks 12–13:

- `models.py` — Task 12's `InstalledPackage`/`InstalledPublication`/`InstalledActivityVersion`/`InstalledQuestionVersion`/`InstalledQuestionGrading`;
- `services.py` — Task 12's `import_package()`/`activate_package()`/`rollback_package()`/`get_active_package()`;
- `views.py` / `urls.py` — Task 13's `LearnView` and the `/learn/<uuid:source_public_id>/` route;
- `tests/` — an established one-file-per-concern convention (`test_models.py`, `test_import_package.py`, `test_activation.py`, `test_rollback.py`, `test_integration.py`, `test_views.py`, `test_offline.py`, plus a shared `conftest.py`/`_wagtail_bootstrap.py`).

Task 14's new models (`OutboxMessage`, `InboxMessage`, `NodeRegistration`) must coexist in `models.py` (or a models package split, decided at implementation-planning time) alongside Task 12's models without modifying them; Task 14's new views/URLs must coexist with Task 13's `LearnView`/route without modifying them; Task 14 tests should follow the existing one-file-per-concern convention (e.g. `test_outbox.py`, `test_ingestion.py`, `test_authentication.py`, `test_transport.py`) rather than one large file.

**Existing `DomainEvent` production paths.** `linuxlab.core.models.DomainEvent` (fields: `event_id`, `event_type`, `aggregate_id`, `payload`, `created_at`, `processed_at`) and `linuxlab.core.events.publish_event(event_type, payload, aggregate_id)` already exist and are already used in production. As of this writing, the **only** event type actually published outside tests is `"activity.completed"`, from exactly two call sites in `linuxlab/learning/services.py` (`claim_activity_completion()` and `submit_attempt()`), both immediately after `Result.objects.create(...)` in the same transaction — this is a strong, obvious candidate for T14-D21's "one real vertical slice," matching the checked-in prompt's own phrasing ("same transaction as authoritative local Result/domain event") closely. This is recorded as a candidate only, per the docs-only instructions; the implementation plan must confirm and select it (or another) explicitly rather than this document deciding it.

**T14-D39** (above) is the one new architectural decision recorded for Task 14's documentation, added 2026-08-30 after the independent review described above. Everything else in this reconciliation section is naming/layout mapping, not a separate decision.

---

## Implementation record (2026-08-30)

Task 14 is implemented, task-by-task per `docs/superpowers/plans/2026-08-30-task14-node-central-event-sync.md`. No semantic change was made to T14-D01 through T14-D39 during implementation; no new architectural decision was invented (no T14-D40).

**Commit range:** `eca5ba6..22e0f88` (11 commits), branched from `abae15b` / Task 13 FINAL PASS `63a2baa2c33f4d8792befa85c1bb6835e3aa8b60`.

```
eca5ba6 feat: add Task 14 sync persistence
0317a90 feat: add versioned sync event contracts
f35f2a4 feat: add per-node sync credentials
9826aa7 feat: enqueue activity completion for sync
66db664 feat: add leased outbox delivery state
1add4c8 feat: add Task 14 sync transports
77107de feat: add idempotent Central inbox ingestion
46dd326 feat: add authenticated sync ingestion API
acdd0ab feat: add Central sync processor boundary
f7611e5 feat: add Node outbox sync worker
22e0f88 test: prove Task 14 event sync end to end
```

**Final module/model names** (all under `linuxlab/sync/`, matching the plan's locked file structure and the checked-in prompt's authoritative naming):

- `models.py` — `NodeRegistration`, `OutboxMessage`, `InboxMessage` (plus Task 12's pre-existing `Installed*` models, unmodified), and two shared trust-boundary helpers reused from Task 12/13's pattern: `_insert_immutable_row()`, `_write_immutable_row_fields()`.
- `contracts.py` — `WireEventEnvelope`, `EventAck`, `BatchAck`, `SyncTransport` (protocol), `SyncEventContract`, `canonical_event_dict()`/`canonical_event_bytes()`, and the transport failure classification `TransportError`/`TransientTransportError`/`AuthTransportError`/`ProtocolTransportError`.
- `registry.py` — `register_sync_event_contract()`, `get_sync_event_contract()`, `get_current_sync_event_contract()`; fail-closed, in-memory, keyed by `(event_type, schema_version)`.
- `authentication.py` — `issue_node_registration()`, `rotate_node_credential()`, `revoke_node_registration()`, `authenticate_node()`, `IssuedNodeCredential`, `NodeAuthenticationError`.
- `enqueue.py` — `enqueue_sync_event()`, `SyncConfigurationError`, and the one registered vertical-slice contract `ACTIVITY_COMPLETED_V1`.
- `outbox.py` — `claim_outbox_batch()`, `mark_delivered()`, `mark_dead_letter()`, `release_transient()`, `release_auth_failure()`, `compute_retry_delay()`, `envelope_for()`, `flush_outbox()`, `FlushSummary`.
- `transports.py` — `HttpSyncTransport` (production), `FileSyncTransport` (dev/test/reference only).
- `ingestion.py` — `ingest_event()`.
- `processing.py` — `SyncEventProcessor` (protocol), `process_inbox_event()`.
- `api.py` — `SyncEventsView`.
- `management/commands/manage_sync_node.py` — `issue`/`rotate`/`revoke` Node credential admin CLI.
- `management/commands/sync_events.py` — `--once` / continuous Node flush worker CLI.

**Migration:** `linuxlab/sync/migrations/0003_noderegistration_inboxmessage_outboxmessage.py` — additive only; no historical migration file was modified; `makemigrations --check --dry-run` is clean on both the `central` and `node` settings profiles.

**Selected real vertical slice:** `activity.completed`, schema_version 1 — the only production `DomainEvent` type actually published as of Task 13 HEAD, from `linuxlab.learning.services.claim_activity_completion()` and `submit_attempt()`, both immediately after `Result.objects.create(...)` in the same `transaction.atomic()` block that also now calls `enqueue_sync_event()`. Exact wire payload fields: `attempt_public_id`, `learner_public_id`, `activity_definition_public_id`, `activity_version`, `score`, `max_score` — a minimal, learner/result-safe subset; no answer key, provider config, grading rationale, or local integer PK is ever included (`aggregate_public_id`/`aggregate_type` and all payload identity fields are stable UUIDs/public values). The guest-claim call site's extra `"source"` payload key is deliberately dropped by the contract's serializer, not forwarded.

**Node credential configuration (environment/settings, never the database):** `LINUXLAB_SYNC_ENABLED`, `LINUXLAB_NODE_ID`, `LINUXLAB_SYNC_SECRET`, `LINUXLAB_CENTRAL_URL`, `LINUXLAB_SYNC_ALLOW_INSECURE_HTTP` (default `false`), `LINUXLAB_SYNC_BATCH_MAX_EVENTS` (default 100), `LINUXLAB_SYNC_BATCH_MAX_BYTES` (default 1,000,000), `LINUXLAB_SYNC_LEASE_SECONDS` (default 300), `LINUXLAB_SYNC_BACKOFF_BASE_SECONDS` (default 5), `LINUXLAB_SYNC_BACKOFF_CAP_SECONDS` (default 3600). Central never stores a Node's plaintext secret — only a Django password hash (`current_secret_hash`/`previous_secret_hash` on `NodeRegistration`), via `django.contrib.auth.hashers.make_password`/`check_password`.

**HTTP route:** `POST /api/v1/sync/events/` (`linuxlab/sync/urls.py`, name `sync-events`), authenticated via `Authorization: Bearer <secret>` + `X-LinuxLab-Node-Id: <uuid>` headers only — never a credential in the JSON body.

**Test coverage for the required proof points** (242 tests total under `linuxlab/sync/tests/`, all passing):

- transaction rollback: `test_task14_enqueue.py::test_business_rollback_rolls_back_domain_event_and_outbox_together`.
- real PostgreSQL concurrency (`select_for_update(skip_locked=True)`, no mocked locking): `test_task14_outbox.py::test_concurrent_claims_never_select_the_same_row`, `test_task14_ingestion.py::test_concurrent_ingest_of_the_same_new_event_id_normalizes_to_one_accepted_one_duplicate`.
- ACK loss / replay: `test_task14_e2e.py::test_commit_then_ack_lost_replay_yields_one_inbox_row_and_eventual_delivery`.
- exact duplicate / idempotency conflict: `test_task14_ingestion.py::test_exact_duplicate_returns_duplicate_with_one_inbox_row`, `test_same_event_id_different_payload_is_idempotency_conflict`.
- authentication (issue/rotate/revoke/grace period/disabled/unknown/wrong-secret, and the 401-vs-403 API distinction): `test_task14_authentication.py`, `test_task14_api.py`.
- lease recovery: `test_task14_outbox.py::test_claim_reclaims_lease_expired_in_flight_row_without_changing_envelope`.
- long-offline recovery: `test_task14_e2e.py::test_repeated_transient_failures_never_delete_or_dead_letter_then_eventually_deliver`.
- `FileSyncTransport` non-delivery: `test_task14_transports.py` (writes/no-secret/unconfirmed-results/empty-batch), `test_task14_flush.py::test_file_transport_flush_leaves_events_unconfirmed_never_delivered`.
- poison-neighbor batch independence: `test_task14_api.py::test_mixed_batch_returns_independent_per_event_results`, `test_task14_flush.py::test_permanent_rejection_only_dead_letters_that_event`, `test_task14_e2e.py::test_poison_neighbor_does_not_block_good_events_in_the_same_batch`.
- source spoofing: `test_task14_api.py::test_source_spoof_is_permanently_rejected_with_no_inbox_row`.
- processor failure after ACK: `test_task14_e2e.py::test_processor_failure_after_ack_never_requires_node_retransmission`.
- no package/Central→Node regression: `test_task14_e2e.py::test_full_cycle_never_touches_package_inventory_or_creates_reverse_transport`.

**Full-suite verification:** `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest -q` → 726 passed (566 pre-Task-14 baseline + 160 new Task 14 tests). `.venv/bin/ruff check .` clean. `manage.py check` and `manage.py makemigrations --check --dry-run` clean on both the `central` and `node` settings profiles.

**One real bug found and fixed during TDD** (Task 5): `claim_outbox_batch()`'s initial implementation fetched all due candidates under `select_for_update(skip_locked=True)` before trimming to the byte budget in Python — but `SELECT FOR UPDATE` locks every row a query returns, not just the ones ultimately selected, so an unbounded fetch over-locked rows beyond `max_events` and starved a concurrent claimer. Fixed by bounding the locked query to `max_events` up front (byte-budget trimming only ever removes from the tail of that same window, so no candidate beyond `max_events` is ever eligible anyway). Caught by a real two-thread PostgreSQL concurrency test, not a mock.

---

## Corrective implementation record (2026-08-31)

Following the independent Task 14 architecture/security review (**verdict B, TASK 14 CONDITIONAL**), all eight Important findings were corrected. No semantic change was made to T14-D01 through T14-D39; the one new decision approved for this corrective is **T14-D40** (above) — no T14-D41 was invented.

**Corrective commit range:** `a528070..1795143` (6 fix commits) plus `e0fd873` (T14-D40 docs) and this record's own commit, all on top of the reviewed HEAD `232e70b3935c72fdccd62075546c1483aa2b860d`.

```
e0fd873 docs: record Task 14 protocol corrective
a528070 fix: enforce Task 14 deployment boundaries
27babb4 fix: seal sync trust record writers
5c21a2f fix: fence stale outbox workers
5a9eb75 fix: recover deterministic sync protocol failures
f32f0f2 fix: normalize and validate sync envelopes
1795143 fix: reject ambiguous sync acknowledgements
```

### Finding 1 — Central-only authority exposed on the Node profile

Added `linuxlab.sync.capabilities.can_receive_sync_events()`/`can_run_sync_worker()`, reading two new fail-closed settings flags (`LINUXLAB_CAN_RECEIVE_SYNC_EVENTS`, `LINUXLAB_CAN_RUN_SYNC_WORKER`) following the existing `LINUXLAB_CAN_*` pattern — never a `LINUXLAB_PROFILE` string comparison. `central.py` sets the first `True`/second `False`; `node.py` the reverse; both default `False` on `base.py` (inherited unchanged by the `test` profile). `SyncEventsView.post()` checks `can_receive_sync_events()` before any credential parsing (403 if absent); `manage_sync_node`'s `handle()` checks the same (`CommandError` if absent); `sync_events`'s `_build_transport()` checks `can_run_sync_worker()` first, before its existing Node ID/secret/Central URL checks. Proven against both the real `linuxlab.settings.central`/`linuxlab.settings.node` modules and simulated cross-profile flag combinations in `test_task14_profiles.py` (7 tests) plus `core/tests/test_settings_profiles.py`'s existing flag-value convention.

### Finding 2 — Immutable-row writer accepted caller-controlled authorization

`_write_immutable_row_fields(model_cls, pk, allowed_fields, **fields)` let every caller construct its own allow-list. Replaced with `_write_immutable_row_fields(instance, **changes)` (single row) and a new `_bulk_write_immutable_rows(queryset, **changes)` (batch), both resolving the mutable-field allow-list from a model-fixed `_MUTABLE_FIELDS_BY_MODEL` mapping inside `linuxlab.sync.models` itself — never from a parameter. The legacy 4-positional-argument call shape no longer exists at all (`TypeError`, not a runtime rejection). `outbox.py`'s own duplicate `_bulk_write_outbox_fields()`/raw `models.QuerySet.update()` bypasses (including inside `claim_outbox_batch()`'s claim transition) were removed and routed through the same shared authority. Permanent tests probe every immutable field on `NodeRegistration`/`OutboxMessage`/`InboxMessage` individually, prove the old signature is `TypeError`-impossible, and confirm every legitimate mutation (rotation/revoke, delivery metadata, processing metadata) still works.

### Finding 3 — Lease recovery lacked stale-worker fencing

Added an additive `claim_token` UUID field to `OutboxMessage` (migration `0004_outboxmessage_claim_token.py`). `claim_outbox_batch()` mints a fresh token per row on every claim and reclaim. `mark_delivered()`/`mark_dead_letter()`/`release_transient()`/`release_auth_failure()` now take the exact claimed row(s), not a bare `event_id`, and write through a new `_write_fenced()` helper filtering on `event_id` + `state=IN_FLIGHT` + that exact `claim_token`; a stale worker's write matches zero rows. Proven with two real two-thread/two-PostgreSQL-connection tests: a stale worker's late transient-failure report cannot resurrect a row a second worker already reclaimed and delivered, and a stale worker's late success report cannot overwrite a row a second worker already reclaimed and dead-lettered.

### Finding 4 — 400/413 retried forever; byte accounting mismatched actual request bytes

Added `serialize_sync_batch()` (`contracts.py`) as the one shared serializer: `claim_outbox_batch()`'s incremental byte-budget accounting and `HttpSyncTransport.send_batch()`'s actual outgoing body now both build from it, closing the gap where the locally enforced count omitted the `{"events":[...]}` wrapper/separator overhead. Exact-boundary tests prove fit-at-N-bytes / excluded-at-N-minus-1-byte, and a dedicated test shows the old naive per-event sum would have (incorrectly) let two events fit under a budget the real wrapped body cannot.

Implemented **T14-D40**: `HttpSyncTransport` raises the new `RequestTooLargeError` (a `ProtocolTransportError` subclass) specifically for 413. `flush_outbox()`'s new `_send_claimed_rows()` splits a multi-event batch on 413 and retries the halves (bounded recursion, O(log n) deep, never unbounded); a singleton that still 413s dead-letters with code `EVENT_TOO_LARGE`. A plain 400 is never treated as `EVENT_TOO_LARGE`; the whole claimed batch releases as retryable (with the existing backoff) and `FlushSummary.protocol_fault` is set so `sync_events` pauses on the same long interval as an auth fault and prints an operator-visible warning, rather than tight-looping the identical malformed request.

### Finding 5 — Incomplete envelope validation; poison events could reach DB exceptions

Added `normalize_wire_event()` (`contracts.py`): the one boundary every inbound envelope crosses before persistence or duplicate comparison. Validates UUID-typed fields are actually UUIDs, bounds `event_type`/`aggregate_type` to their model `CharField` `max_length`s (255/100), checks `aggregate_type` against a new `SyncEventContract.expected_aggregate_type`, and cross-checks `aggregate_public_id` against a new `SyncEventContract.payload_aggregate_public_id()` extractor (for `activity.completed`, the payload's own `attempt_public_id`). Any failure raises `WireEventValidationError`, mapped uniformly by `ingest_event()` to `REJECTED`/`INVALID_PAYLOAD` — never a 500, never an uncaught `DataError`. `ingest_event()`'s `IntegrityError` handling was also narrowed: on a unique-constraint race it now confirms a row with that exact `event_id` exists before classifying duplicate/conflict; any other `IntegrityError` cause re-raises as a genuine infrastructure fault. `activity.completed`'s own payload validator now parses its three identity fields as actual UUIDs, not just non-empty strings.

### Finding 6 — Timestamp normalization broke exact replay idempotency

`normalize_wire_event()` UTC-normalizes `occurred_at` (`.astimezone(UTC)`) and rejects naive timestamps outright rather than guessing a Node's timezone. Central persists and compares using only the normalized form, so a retry encoding the same instant with a different UTC offset (+07:00 vs Z vs +00:00) is recognized as `DUPLICATE`, never `IDEMPOTENCY_CONFLICT`. Proven at the unit level (`test_task14_validation.py`, `test_task14_ingestion.py::test_replay_with_different_utc_offset_for_the_same_instant_is_duplicate_not_conflict`) and end-to-end through the real `flush_outbox()` → `authenticate_node()`/`ingest_event()` path with a lost-ACK retry (`test_task14_e2e.py::test_ack_lost_replay_with_non_utc_offset_becomes_duplicate_not_conflict`).

### Finding 7 — Duplicate/incomplete ACK entries could authorize irreversible action

Added `parse_ack_results()` (`contracts.py`): the one shared, conservative ACK parser. An `event_id` appearing more than once among otherwise-valid candidates drops *all* of its entries (ambiguous, stays unconfirmed); a `REJECTED` entry with no rejection code is dropped the same way. `HttpSyncTransport` delegates to it instead of building its own list; `BatchAck.status_for()` is hardened identically for any direct caller. `flush_outbox()`'s own ACK consumption is independently hardened too (never trusting that a hand-built `BatchAck` is already deduplicated) — defense in depth, not redundant, since existing tests already construct `BatchAck` directly. Proven with a full ACK ambiguity matrix (duplicate/conflicting entries, missing code, unknown IDs, mixed valid/malformed) at both the transport-parser and flush-orchestration layers.

### Finding 8 — Plaintext Node credential appeared in repr

`IssuedNodeCredential.secret` is now `dataclasses.field(repr=False)`. The CLI's one authorized plaintext disclosure (`manage_sync_node issue`/`rotate`, printed once to the operator via `self.stdout.write(f"secret: {credential.secret}")`) is unaffected since it reads the attribute directly, never through `repr()`.

### Profile authority (final)

| | Central | Node |
|---|---|---|
| Ingestion (`POST /api/v1/sync/events/`) | ✅ | ❌ (403) |
| Provisioning (`manage_sync_node`) | ✅ | ❌ (`CommandError`) |
| Outbound worker (`sync_events`) | ❌ (`CommandError`) | ✅ |

### Verification

- Focused sync suite: `linuxlab/sync/tests` — **320 passed**.
- Complete suite: `env -u DJANGO_SETTINGS_MODULE pytest -q` — **806 passed** (726 pre-corrective baseline + 80 new corrective tests).
- `ruff check .` — clean.
- `manage.py check` (central, node) — clean.
- `manage.py makemigrations --check --dry-run` (central, node) — no drift beyond the checked-in additive `0004_outboxmessage_claim_token` migration.

### Minor backlog (unchanged, not addressed by this corrective)

Dead backoff settings (`LINUXLAB_SYNC_BACKOFF_BASE_SECONDS`/`CAP_SECONDS` still unused by `release_transient()`), `FileSyncTransport` deterministic-output hardening, a durable idempotency-conflict audit trail, and a claim-path index/schema-version constraint remain backlog — none were mechanically necessary to fix an Important finding, so none were touched.
