# Task 14 Node → Central Event Synchronization Design

**Date:** 2026-08-30
**Status:** APPROVED DESIGN — implementation has not started
**Depends on:** Task 13 FINAL PASS at `63a2baa2c33f4d8792befa85c1bb6835e3aa8b60`
**Decision log:** `docs/devlog/2026-08-30-task14-sync-decisions.md`
**Authoritative checked-in prompt:** `docs/codex/task-prompts/TASK_14_idempotent-outbox-inbox-event-sync.md` — see §6/§13/§30 and the decision log's "Repository reality reconciliation" for how its exact naming (`OutboxMessage`, `InboxMessage`, `SyncTransport`, `HttpSyncTransport`, `FileSyncTransport`, `flush_outbox()`, `/api/v1/sync/events/`) maps onto this spec's more conceptual naming (`SyncOutbox`, `SyncInbox`).

## 1. Purpose

Task 14 adds reliable offline-first synchronization of selected Node-originated events to Central.

The task closes the Phase 1 transport boundary between offline Nodes and Central without introducing database replication or a bidirectional event bus.

Task 14 has one direction only:

```text
Node → Central

DomainEvent
    ↓ transactional SyncOutbox
    ↓ outbound HTTPS batch push
    ↓ authenticated Central ingestion
    ↓ durable SyncInbox
    ↓ processor contract
```

Central → Node content continues to use the already accepted deployment mechanism:

```text
Central PublishedVersion
    ↓ signed immutable .llpkg
    ↓ Node verify
    ↓ stage
    ↓ activate
```

The two mechanisms are intentionally distinct.

## 2. Architectural goals

Task 14 must provide:

- at-least-once Node → Central transport;
- effectively-once Central ingestion through event UUID idempotency;
- stable versioned wire envelopes;
- transactional Outbox creation;
- safe concurrent Node workers;
- bounded batching;
- per-event ACK;
- transient retry and permanent dead-letter behavior;
- per-Node authentication;
- fail-closed event registry;
- durable immutable Central Inbox;
- a downstream processor boundary;
- one real event vertical slice proving the entire pipeline.

## 3. Non-goals

Task 14 does not implement:

- Central → Node event sync;
- package auto-download;
- `.llpkg` replacement;
- database replication;
- Central learning projection into `Attempt`, `Result`, or `Progress`;
- global event ordering;
- self-service Node enrollment;
- dead-letter repair UI;
- automatic retention/GC;
- Celery/Redis/RabbitMQ/Kafka;
- Node inbound Internet endpoints;
- production air-gap/USB synchronization workflow (file authenticity/integrity trust, Central import authorization, operator transfer workflow, ACK/result round-trip, reconciliation after delayed import — see T14-D39 and §13.2).

## 4. Deployment model

### 4.1 Node

Node remains offline-capable.

Node may perform all local learning behavior with synchronization disabled.

When synchronization is enabled, the Node must already possess:

- Central-issued immutable `node_id`;
- local bearer credential;
- Central base URL;
- sync-enabled configuration.

No Internet connection is required to create local business/domain state or to create Outbox events.

### 4.2 Central

Central owns:

- Node registrations;
- bearer credential hashes;
- sync endpoint;
- Inbox;
- event-schema registry;
- processor contract.

Central does not need to connect inbound to Nodes.

## 5. High-level architecture

```text
NODE
────────────────────────────────────────────────────

Business/Application Service
    ↓ transaction.atomic()
        - domain mutation
        - DomainEvent
        - enqueue_sync_event()
            → immutable SyncOutbox(PENDING)
    ↓ COMMIT

Sync Worker
    ↓ claim eligible bounded batch
        (PENDING → IN_FLIGHT, short DB transaction)
    ↓ HTTPS POST outside DB transaction
    ↓ per-event ACK
        - accepted/duplicate      → DELIVERED
        - permanent reject        → DEAD_LETTER
        - transient/unconfirmed   → PENDING + backoff

                    ↓ HTTPS Bearer

CENTRAL
────────────────────────────────────────────────────

Node Authentication
    ↓ authenticated NodeRegistration

Batch Ingestion Endpoint
    - request/batch validation
    - event-level durable ingestion
    ↓

SyncInbox
    - immutable normalized envelope
    - UNIQUE event_id
    - received_at
    - processing metadata
    ↓

Processor Contract
    - domain projections deferred
```

## 6. Module boundaries

Task 14 remains in the existing `linuxlab.sync` Django app.

Conceptual structure:

```text
linuxlab/sync/
├── models.py               # persistent NodeRegistration / Outbox / Inbox
├── registry.py             # syncable event contracts
├── enqueue.py              # DomainEvent → immutable Outbox
├── outbox.py               # claim, lease, retry state transitions
├── transport.py            # SyncTransport interface + HttpSyncTransport/FileSyncTransport
├── authentication.py       # Central per-Node bearer verification
├── ingestion.py             # Central normalized Inbox ingestion
├── processing.py            # processor interface/state only
├── views.py                # thin HTTP adapters
├── urls.py
└── management/commands/
    └── sync_events.py       # flush_outbox() entry point
```

This is a responsibility design, not a mandate to create unrelated files if the existing repository already has an equivalent focused module. `linuxlab/sync/models.py` and `services.py` already contain Task 12's `InstalledPackage`/lifecycle code, and `views.py`/`urls.py` already contain Task 13's `LearnView`/`/learn/<uuid>/` route; Task 14 additions must coexist with these without modifying them. See the decision log's "Repository reality reconciliation" for the exact naming this maps to (`OutboxMessage`, `InboxMessage`, `SyncTransport`, `flush_outbox()`, `/api/v1/sync/events/` per the checked-in Task 14 prompt).

No new Django app is required.

## 7. Event wire envelope

The wire protocol is separate from the persistence representation of `DomainEvent`.

Minimum normalized envelope:

```text
event_id
event_type
occurred_at
source_node_id
aggregate_type
aggregate_public_id
schema_version
payload
```

### 7.1 Event ID

`event_id` is a globally unique immutable UUID.

Retry never creates a replacement event UUID.

### 7.2 Event type and schema

Compatibility is determined by exact:

```text
(event_type, schema_version)
```

Central and Node use an explicit registry.

Unknown type/version is not guessed or migrated during ingestion.

### 7.3 Aggregate identity

`aggregate_type` and `aggregate_public_id` identify the domain object/event source without exposing local database PKs as cross-node identity.

### 7.4 Time

`occurred_at` comes from Node and is preserved as metadata.

Central independently creates `received_at`.

`occurred_at` does not authorize, deduplicate, prevent replay, or establish global ordering.

## 8. Syncable event registry

Synchronization is fail-closed.

Default:

```text
DomainEvent → LOCAL-ONLY
```

Only explicitly registered `(event_type, schema_version)` entries may enter `SyncOutbox`.

A registry entry defines the versioned serialization/validation contract.

Task 14 wires exactly one existing real repository event end-to-end as the reference vertical slice.

The implementation planner must inspect repository reality before choosing that event. As of Task 13 (`63a2baa`), the only production `DomainEvent` type actually published is `"activity.completed"` (see the decision log's repository reality section) — a strong candidate, not a selection made by this document.

## 9. Transactional Outbox

### 9.1 Creation invariant

For a syncable domain action:

```text
transaction.atomic()
    domain mutation
    DomainEvent insert
    SyncOutbox insert
commit
```

All three succeed or all three roll back.

Network is never part of this transaction.

No Django signal or post-commit scanner creates the Outbox record.

### 9.2 Outbox event envelope

Immutable after insert:

```text
event_id
event_type
schema_version
source_node_id
occurred_at
aggregate_type
aggregate_public_id
payload
```

Retry must resend the stored envelope.

### 9.3 Delivery metadata

Mutable only through sync delivery services:

```text
state
attempts
next_attempt_at
claimed_at
lease_until
delivered_at
rejection_code
rejection_reason
created_at
```

Business code cannot modify the immutable wire envelope.

## 10. Outbox state machine

```text
PENDING
  → claim
IN_FLIGHT
  ├─ accepted/duplicate → DELIVERED
  ├─ permanent reject   → DEAD_LETTER
  ├─ transient failure  → PENDING
  └─ expired lease      → reclaimable
```

Dead-letter events are retained.

Delivered rows are retained.

Task 14 does not implement automatic deletion/GC.

## 11. Worker concurrency and leasing

Node sync worker is a dedicated process, conceptually a Django management command.

It may support one-shot execution for testing/administration and continuous execution under systemd or equivalent.

Eligible rows:

```text
state = PENDING
next_attempt_at <= now
```

Claim policy is deterministic and fair:

```text
ORDER BY
next_attempt_at ASC,
created_at ASC,
local_pk ASC
```

Concurrency uses a short PostgreSQL claim transaction, conceptually:

```text
SELECT ... FOR UPDATE SKIP LOCKED
```

Claim updates rows to `IN_FLIGHT` with a lease.

The transaction commits before network I/O begins.

Expired leases make events reclaimable.

This scheduling policy is not a transport ordering guarantee.

## 12. Batch construction

A batch is bounded by both:

- configured maximum event count;
- configured maximum serialized byte size.

The first limit reached ends the batch.

Limits are runtime configuration, not hard-coded into the event contract.

## 13. Transport

### 13.1 Direction

Node initiates outbound requests.

### 13.2 Transport abstraction

Transport is an explicit interface (`SyncTransport` per the checked-in prompt), not hard-coded HTTP: ADR-010 states "Transport is independent from the package/event format so HTTPS, LAN, file, or later USB workflows can use the same contracts." Task 14 provides two implementations against the same envelope, with clearly distinct scope (T14-D39):

```text
SyncTransport
├── HttpSyncTransport
│   └── production Node → Central transport (§13.3, §13.4)
└── FileSyncTransport
    └── dev/test/reference transport in Task 14 (§13.3)
```

**`HttpSyncTransport` is the only production Node → Central transport in Task 14.** Production delivery is defined as: HTTPS push → Central authenticated ingestion → durable `SyncInbox` commit (or exact duplicate) → valid per-event ACK → `DELIVERED`. No other path produces `DELIVERED` in production.

**`FileSyncTransport` exists only because the checked-in prompt requires the transport abstraction and a file implementation**, and is limited in Task 14 to: development; tests; reference implementation of the `SyncTransport` interface; deterministic fixtures/local export inspection. It uses exactly the same event envelope `HttpSyncTransport` would send, which is what makes it useful as a reference implementation and as offline/USB-style integration-test scaffolding — but Task 14 does **not** implement the additional production air-gap protocol a real USB/offline sync workflow would need: file authenticity/integrity trust, Central import authorization, an operator transfer workflow, durable Inbox acceptance of an imported file, an ACK/result round-trip back to the originating Node, or reconciliation after a delayed import. That protocol is out of scope (§3) and is not designed here.

**A successful local file write is not delivery.** `FileSyncTransport` completing a write proves the envelope serialized correctly; it proves nothing about whether Central has durably accepted the event. No code path may transition an Outbox event to `DELIVERED` on the strength of a `FileSyncTransport` write alone in production semantics — only a confirmed per-event ACK from real Central ingestion does that (T14-D04, T14-D30).

### 13.3 Production security

Production requires HTTPS with normal certificate/hostname validation.

There is no automatic HTTP fallback.

Explicit HTTP may exist in development/test only. `FileSyncTransport` is not a production HTTPS fallback and is not a production USB/air-gap sync path — it is a distinct, explicitly-selected dev/test/reference transport (§13.2), and does not weaken the HTTPS-only production policy or the durable-Inbox-before-ACK boundary (T14-D04, T14-D12).

### 13.4 Authentication

Node sends:

- immutable Node identity metadata;
- opaque bearer secret.

Central looks up `NodeRegistration` by immutable Node ID and verifies the bearer secret against stored password hashes.

The authenticated registration is the trusted identity.

Each event's `source_node_id` must match it.

## 14. NodeRegistration and credential lifecycle

Central provisions immutable Node UUID identity.

Conceptual fields:

```text
node_id
display_name
current_secret_hash
previous_secret_hash
previous_valid_until
enabled
created_at
rotated_at
revoked_at
```

Plaintext secret is returned only during initial issuance/rotation and is not persisted by Central.

Node stores plaintext credential outside its application database and source repository.

### 14.1 Normal rotation

```text
new secret → current
old current → previous
previous_valid_until = configured grace
```

Both may authenticate during the bounded grace period.

### 14.2 Emergency revoke

Revocation disables current and previous credentials immediately.

## 15. Central Inbox

`SyncInbox` is the durable acceptance ledger.

Conceptual immutable event fields:

```text
event_id UNIQUE
source_node
event_type
schema_version
occurred_at
received_at
aggregate_type
aggregate_public_id
payload
```

Mutable processing metadata:

```text
processing_state
processed_at
processing_error
```

Event envelope fields cannot be overwritten after ingestion.

## 16. Central event ingestion

Request-level steps:

1. authenticate Node;
2. validate request/batch shape and configured limits.

Then each event is handled independently.

For one event:

1. validate source Node claim;
2. validate registered event type/version;
3. validate payload;
4. check `event_id`;
5. insert or classify duplicate/conflict.

### 16.1 New event

Durably insert normalized Inbox event.

After commit:

`ACCEPTED`

### 16.2 Exact duplicate

If existing `event_id` has exactly the same immutable normalized content:

`DUPLICATE`

Duplicate is a successful acknowledgement.

### 16.3 Idempotency conflict

If existing `event_id` has different immutable content:

`IDEMPOTENCY_CONFLICT`

Central:

- keeps original Inbox unchanged;
- records/audits conflict;
- permanently rejects the incoming event.

Node dead-letters it.

No overwrite, merge, or last-write-wins.

## 17. ACK and HTTP semantics

HTTP status describes the request/transport layer.

Examples:

```text
401/403 → auth/channel failure
400     → malformed request/batch
413     → request exceeds accepted batch limits
5xx     → transient Central failure
2xx     → authenticated structurally valid batch with per-event results
```

Per-event results:

```text
accepted
duplicate
rejected
```

Node marks only explicitly confirmed `accepted`/`duplicate` event IDs as delivered.

Missing or malformed results are unconfirmed and remain retryable.

## 18. Failure taxonomy

### 18.1 Authentication/channel failure

401/403:

- do not dead-letter events;
- release/preserve retryable events;
- pause/degrade worker;
- use long backoff;
- require operator/credential recovery.

### 18.2 Transient transport

Timeout, DNS, connection refusal, network loss, 5xx, missing ACK:

- retry;
- exponential backoff;
- jitter;
- configurable maximum delay;
- no finite retry-count dead-letter.

### 18.3 Request protocol

Malformed/oversized request:

- request-level error;
- Node does not infer delivery for unconfirmed events.

### 18.4 Permanent event rejection

Unsupported type/version, invalid payload, source spoof, idempotency conflict:

- event → DEAD_LETTER;
- no automatic retry.

### 18.5 Central processing failure

The Inbox event is already accepted.

Processor failure does not cause Node retransmission.

## 19. Retry and backoff

Transient retry uses exponential backoff with jitter and a cap.

The specific schedule is configurable.

The protocol does not depend on a fixed number of retry attempts.

Node may remain offline for days and resume later without data loss due only to retry count.

## 20. Central processing boundary

Durable ingestion and domain application are separate.

Inbox state is conceptually:

```text
RECEIVED
  → PROCESSED
  → FAILED
```

Task 14 defines the processor interface/boundary only.

Task 14 does not mutate Central `Attempt`, `Result`, `Progress`, or other learning aggregates.

## 21. Synchronization enablement

### Sync disabled

Local Node operation continues.

No Outbox is produced for sync.

No automatic backfill is promised for events produced before enrollment/enablement.

### Sync enabled

Central-issued stable Node UUID is mandatory.

Missing Node identity is a configuration error and must fail loudly for syncable event enqueue.

A temporarily invalid secret affects transport only; it does not prevent safe local Outbox creation.

## 22. Delivery semantics

Formal statement:

```text
at-least-once transport
+
globally unique immutable event_id
+
durable Inbox UNIQUE(event_id)
+
exact duplicate comparison
=
effectively-once ingestion
```

Task 14 does not claim exactly-once transport.

## 23. Ordering

No global ordering guarantee exists.

Fair Outbox scheduling is not protocol ordering.

If a later domain requires aggregate order/version semantics, that domain must define them explicitly.

## 24. Security invariants

1. Trusted Node identity comes from authenticated `NodeRegistration`.
2. Envelope `source_node_id` must equal authenticated Node identity.
3. Production sync is HTTPS-only.
4. Central stores bearer secrets only as password hashes.
5. Plaintext secret remains Node-local after issuance.
6. Current/previous credential overlap is bounded by grace period.
7. Emergency revoke is immediate.
8. Syncable events are allowlisted by exact type/version.
9. Outbox event content is immutable after insertion.
10. Inbox event content is immutable after acceptance.
11. Same ID with changed content is a conflict, never an overwrite.
12. Task 11 package signing keys are not Node auth credentials.
13. No database replication is introduced.
14. No learner/server grading material is implicitly synchronized merely because a DomainEvent exists; only explicit sync registry payloads may leave Node.
15. Bearer secrets and stored hashes must not be logged or returned in API responses.

## 25. Real vertical slice

Task 14 must demonstrate one real existing domain event through:

```text
real business action
    ↓ same-transaction DomainEvent
    ↓ same-transaction SyncOutbox
    ↓ Node claim
    ↓ HTTPS protocol/client
    ↓ Central auth
    ↓ Inbox durable insert
    ↓ ACK
    ↓ Node DELIVERED
```

The exact event type is selected only after repository inspection.

This vertical slice proves infrastructure correctness without making all events syncable.

## 26. Testing strategy

### 26.1 Unit

- registry allowlist and version lookup;
- event payload validation;
- envelope normalization;
- retry/backoff calculations;
- ACK parsing;
- credential verification;
- exact duplicate content comparison.

### 26.2 Database/state machine

- domain transaction rollback produces no Outbox;
- sync-disabled behavior;
- sync-enabled missing Node ID fails;
- Outbox immutable envelope;
- controlled delivery transitions;
- concurrent claims do not double-claim rows;
- lease expiry allows recovery;
- Inbox unique event identity;
- exact duplicate vs content conflict;
- immutable Inbox envelope.

### 26.3 Protocol/security

- bad bearer secret;
- disabled/revoked registration;
- previous credential inside and outside grace period;
- source Node spoof;
- unknown type;
- unsupported schema;
- malformed payload;
- partial/malformed ACK;
- 401/403 does not dead-letter;
- production HTTPS enforcement;
- secret/hash non-disclosure;
- `FileSyncTransport` round-trips the identical envelope `HttpSyncTransport` would send, proving transport independence.

### 26.4 End-to-end

At least one test should prove the real vertical slice through actual Task 14 production boundaries.

Load-bearing scenarios:

```text
Central commit + ACK lost
    → Node retries
    → duplicate
    → one Inbox row
    → Node DELIVERED

two workers
    → one Outbox row claimed once

worker crash
    → lease expires
    → reclaim

one poison event
    → valid neighbors still accepted

same event_id + changed payload
    → conflict
    → original Inbox unchanged

offline for long period
    → event retained
    → later delivery succeeds

processor failure after ACK
    → Node stays delivered
    → Inbox retained for Central handling
```

## 27. Retention

Task 14 performs no automatic hard delete of:

- delivered Outbox events;
- dead letters;
- Inbox events.

Retention/GC is a later operational feature.

## 28. Operational model

Node runs a dedicated sync worker process.

The worker is externally supervised, for example by systemd.

Task 14 does not require Celery, Redis, RabbitMQ, or Kafka.

## 29. Migration expectations

Task 14 is expected to add database models and therefore migrations in the existing `linuxlab.sync` app.

Migrations must be additive and must not rewrite or alter accepted Task 11/12/13 historical migrations.

Exact constraints/indexes must be specified during implementation planning after inspecting current `sync` models.

## 30. Documentation and implementation gate

Before implementation planning:

1. reconcile this spec with the authoritative checked-in Task 14 prompt and current repository (see the decision log's "Repository reality reconciliation" — naming, `FileSyncTransport`, existing `linuxlab/sync/` layout, and the `"activity.completed"` vertical-slice candidate are already reconciled there; no material conflict was found);
2. report any material conflict instead of silently choosing one;
3. keep T14-D01 through T14-D38 unchanged unless the human explicitly approves a replacement;
4. perform spec self-review for placeholders, contradictions, scope leaks, and ambiguity;
5. obtain explicit human approval of the written spec.

Only after written-spec approval may the implementation plan be created.
