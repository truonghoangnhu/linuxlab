# Task 14 Node → Central Event Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement reliable offline-first Node → Central event synchronization with a transactional Outbox, authenticated HTTPS batching, durable idempotent Inbox ingestion, safe retry/lease semantics, and one real `activity.completed` vertical slice.

**Architecture:** Extend the existing `linuxlab.sync` Django app. Node business transactions explicitly write `DomainEvent` + immutable `OutboxMessage`; a dedicated worker claims bounded batches and pushes them through `HttpSyncTransport`. Central authenticates a `NodeRegistration`, ingests each event independently into immutable `InboxMessage`, and ACKs only after durable acceptance or exact duplicate detection. `FileSyncTransport` is dev/test/reference only and must never by itself establish production delivery.

**Tech Stack:** Python 3.12, Django 5.2, PostgreSQL 18, Django password hashers, Python standard-library HTTPS/JSON/crypto utilities, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-30-task14-node-central-event-sync-design.md`

## Global Constraints

- Start from `abae15b`; Task 13 is FINAL PASS at `63a2baa2c33f4d8792befa85c1bb6835e3aa8b60`.
- Accepted pre-Task-14 baseline: **566 passing tests**.
- Preserve `T14-D01` through `T14-D39`.
- Task 14 is **Node → Central event sync only**.
- Central → Node content remains signed immutable `.llpkg`; no package auto-download or DB replication.
- Transport is **at-least-once**; only Inbox durability + UUID idempotency provides effectively-once ingestion.
- Never claim exactly-once transport.
- `event_id` is immutable and reused across retries.
- No global ordering guarantee.
- The wire envelope is separate from `DomainEvent` persistence.
- Sync is fail-closed: unregistered event types are LOCAL-ONLY.
- Business mutation + `DomainEvent` + `OutboxMessage` are committed in one PostgreSQL transaction for the selected vertical slice.
- No network I/O inside the business transaction.
- Outbox event envelope is immutable after insert; only controlled delivery metadata may change.
- Inbox event envelope is immutable after acceptance; only processing metadata may change.
- ACK only after durable Inbox commit or confirmed exact duplicate.
- Same `event_id` with changed immutable content is `IDEMPOTENCY_CONFLICT`; never overwrite.
- One poison event must not roll back good events in the same batch.
- Node auth uses Central-issued stable `node_id` + opaque bearer secret.
- Central stores only password hashes; no plaintext bearer secret storage.
- Production HTTP transport is HTTPS-only with certificate verification and no HTTP fallback.
- `FileSyncTransport` is **dev/test/reference only** in Task 14 and never proves production delivery.
- Transient failures retry indefinitely with exponential backoff + jitter + cap.
- 401/403 is a channel fault; do not dead-letter individual events.
- Permanent event rejection transitions only that event to `DEAD_LETTER`.
- Central processor success is not part of the Node ACK boundary.
- Task 14 does not mutate Central `Attempt`, `Result`, or `Progress`.
- No Celery, Redis, RabbitMQ, Kafka, background web thread, or global event sequencer.
- Task 14 may add additive migrations in `linuxlab.sync`; do not edit accepted historical migrations.
- The authoritative checked-in prompt is `docs/codex/task-prompts/TASK_14_idempotent-outbox-inbox-event-sync.md`. If repository reality materially conflicts with this plan/spec, STOP and report instead of improvising.

---

## File Structure Locked by This Plan

Use authoritative Task 14 names where they exist:

```text
linuxlab/sync/
├── models.py
│   ├── existing Task 12 Installed* models
│   ├── NodeRegistration
│   ├── OutboxMessage
│   └── InboxMessage
├── contracts.py
│   ├── WireEventEnvelope
│   ├── SyncEventContract
│   ├── EventAck
│   ├── BatchAck
│   └── SyncTransport protocol
├── services.py
│   └── retain existing Task 12 services; add only focused public orchestration if authoritative prompt requires it
├── registry.py
│   └── fail-closed syncable-event registry
├── enqueue.py
│   └── explicit DomainEvent → immutable OutboxMessage
├── outbox.py
│   └── claim/lease/retry/delivery state machine
├── transports.py
│   ├── HttpSyncTransport
│   └── FileSyncTransport
├── authentication.py
│   └── Node credential issue/rotate/revoke/verify
├── ingestion.py
│   └── durable Inbox event-level ingestion
├── processing.py
│   └── processor contract/state transitions only
├── api.py
│   └── `/api/v1/sync/events/` HTTP adapter
├── urls.py
│   └── existing learner route + sync API route
└── management/commands/
    ├── sync_events.py
    └── manage_sync_node.py
```

If an exact equivalent focused module already exists, adapt file placement mechanically, but do not collapse transport, ingestion, authentication, enqueue, and state-machine responsibilities into one oversized module.

**Mechanical note (this copy):** the coordinator-supplied plan text is otherwise reproduced verbatim below; only encoding artifacts (arrows/dashes rendered as `â`) were corrected, and settings-module command placeholders were replaced with this repository's actual module paths (`linuxlab.settings.central` / `linuxlab.settings.node`).

---

### Task 1: Persistent sync models, constraints, and immutability guards

**Files:**
- Modify: `linuxlab/sync/models.py`
- Create: next additive `linuxlab/sync/migrations/000X_task14_event_sync.py`
- Create: `linuxlab/sync/tests/test_task14_models.py`

**Interfaces:**
- Produces:
  - `NodeRegistration`
  - `OutboxMessage`
  - `InboxMessage`
  - explicit state constants/choices for Outbox and Inbox processing
- Existing Task 12 Installed* models and migrations remain unchanged.

- [ ] **Step 1: Write RED model/constraint tests**

Cover at least:

```python
def test_node_registration_node_id_is_unique(db):
    first = NodeRegistration.objects.create(...)
    with pytest.raises(IntegrityError):
        NodeRegistration.objects.create(node_id=first.node_id, ...)


def test_outbox_event_id_is_unique(db):
    event_id = uuid4()
    OutboxMessage.objects.create(event_id=event_id, ...)
    with pytest.raises(IntegrityError):
        OutboxMessage.objects.create(event_id=event_id, ...)


def test_inbox_event_id_is_unique(db):
    event_id = uuid4()
    InboxMessage.objects.create(event_id=event_id, ...)
    with pytest.raises(IntegrityError):
        InboxMessage.objects.create(event_id=event_id, ...)
```

Also write tests proving:
- Outbox state only allows `PENDING`, `IN_FLIGHT`, `DELIVERED`, `DEAD_LETTER`.
- Inbox processing state only allows `RECEIVED`, `PROCESSED`, `FAILED`.
- required timestamps/positive schema versions are constrained.
- envelope field update attempts are rejected after insert.
- delivery metadata may be changed only through the private delivery writer introduced later; public model/queryset update paths must not be able to rewrite event identity/payload.
- Inbox immutable event fields cannot be overwritten after acceptance.
- historical Task 12 rows remain readable.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_models.py -v
```

Expected: import/model failures because Task 14 models do not exist.

- [ ] **Step 3: Implement additive models and guards**

Use exact authoritative model names. Follow established Task 12 guarded-queryset/base-manager patterns rather than inventing a second ORM-guard style. Public ORM must not rewrite immutable envelope fields.

- [ ] **Step 4: Create additive migration and run model tests**

```bash
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py makemigrations sync
.venv/bin/pytest linuxlab/sync/tests/test_task14_models.py -v
```

Inspect the generated migration. It must be additive only and must not modify historical migration files.

- [ ] **Step 5: Run Task 12/13 sync regression**

```bash
.venv/bin/pytest linuxlab/sync/tests -v
```

- [ ] **Step 6: Commit**

```bash
git add linuxlab/sync/models.py linuxlab/sync/migrations linuxlab/sync/tests/test_task14_models.py
git commit -m "feat: add Task 14 sync persistence"
```

---

### Task 2: Wire contracts, canonical normalization, and fail-closed event registry

**Files:**
- Create: `linuxlab/sync/contracts.py`
- Create: `linuxlab/sync/registry.py`
- Create: `linuxlab/sync/tests/test_task14_contracts.py`
- Create: `linuxlab/sync/tests/test_task14_registry.py`

**Interfaces:**
- Produces:
  - `WireEventEnvelope`
  - `SyncEventContract`
  - `EventAck`
  - `BatchAck`
  - `SyncTransport`
  - `canonical_event_dict(envelope)`
  - `canonical_event_bytes(envelope)`
  - `get_sync_event_contract(event_type, schema_version)`
  - `register_sync_event_contract(contract)`
- Registry absence means LOCAL-ONLY on Node / unsupported on Central.

- [ ] **Step 1: Write RED contract tests**

Define exact frozen contract shapes (`WireEventEnvelope` with `event_id`, `event_type`, `occurred_at`, `source_node_id`, `aggregate_type`, `aggregate_public_id`, `schema_version`, `payload`). ACK statuses: `accepted`, `duplicate`, `rejected`. `EventAck` must include `event_id`, `status`, optional rejection `code` and safe `reason`. `BatchAck` must expose a result lookup by `event_id` without treating missing IDs as delivered. Write tests that canonicalization is deterministic and that changing any immutable event field changes canonical bytes.

- [ ] **Step 2: Write RED registry tests**

Required: unregistered event returns `None` (LOCAL-ONLY); registry is keyed by exact `(event_type, schema_version)`; registry must reject duplicate registration of the same key.

- [ ] **Step 3: Run RED**

```bash
.venv/bin/pytest \
  linuxlab/sync/tests/test_task14_contracts.py \
  linuxlab/sync/tests/test_task14_registry.py -v
```

- [ ] **Step 4: Implement minimal contracts and registry**

`SyncEventContract` should provide explicit callables for serializing one existing `DomainEvent` into event-specific payload/aggregate identity, and validating a wire payload at Central. Do not serialize arbitrary `DomainEvent.__dict__`. Use immutable/frozen data structures for contracts.

- [ ] **Step 5: Run GREEN and Ruff**

```bash
.venv/bin/pytest \
  linuxlab/sync/tests/test_task14_contracts.py \
  linuxlab/sync/tests/test_task14_registry.py -v
.venv/bin/ruff check linuxlab/sync/contracts.py linuxlab/sync/registry.py
```

- [ ] **Step 6: Commit**

```bash
git add linuxlab/sync/contracts.py linuxlab/sync/registry.py linuxlab/sync/tests/test_task14_contracts.py linuxlab/sync/tests/test_task14_registry.py
git commit -m "feat: add versioned sync event contracts"
```

---

### Task 3: Node registration credential lifecycle and Central authentication

**Files:**
- Create: `linuxlab/sync/authentication.py`
- Create: `linuxlab/sync/tests/test_task14_authentication.py`
- Create: `linuxlab/sync/management/commands/manage_sync_node.py`

**Interfaces:**
- Produces:
  - `issue_node_registration(display_name) -> IssuedNodeCredential`
  - `rotate_node_credential(node_id, grace_period) -> IssuedNodeCredential`
  - `revoke_node_registration(node_id) -> None`
  - `authenticate_node(node_id, bearer_secret, now=None) -> NodeRegistration`
- `IssuedNodeCredential` contains plaintext secret only in the return value; plaintext is never persisted.

- [ ] **Step 1: Write RED credential tests**

Required: issue generates high-entropy opaque secret and stores only Django password hash; `check_password(secret, current_secret_hash)` succeeds; plaintext does not appear in DB fields/repr; rotation moves old hash to previous and sets bounded `previous_valid_until`; old secret works inside grace; old secret fails outside grace; new secret works immediately; revoke makes both current and previous invalid immediately; disabled registration fails; wrong/unknown node ID fails without leaking whether secret was close/correct.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_authentication.py -v
```

- [ ] **Step 3: Implement credential services**

Use `secrets.token_urlsafe(32)`, `django.contrib.auth.hashers.make_password`, `django.contrib.auth.hashers.check_password`. Do not use JWT, encryption-at-rest of recoverable plaintext, Task 11 signing keys, or Django `SECRET_KEY` as the Node credential. Return a frozen value object such as `IssuedNodeCredential(node_id, secret)`.

- [ ] **Step 4: Implement controlled management command**

Provide explicit operations: `issue --name "Lab A"`, `rotate <node_uuid> --grace-hours 24`, `revoke <node_uuid>`. The command may print the newly issued plaintext secret once to the operator. It must not log credential hashes or old secrets.

- [ ] **Step 5: Run GREEN**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_authentication.py -v
.venv/bin/ruff check linuxlab/sync/authentication.py linuxlab/sync/management/commands/manage_sync_node.py
```

- [ ] **Step 6: Commit**

```bash
git add linuxlab/sync/authentication.py linuxlab/sync/management/commands/manage_sync_node.py linuxlab/sync/tests/test_task14_authentication.py
git commit -m "feat: add per-node sync credentials"
```

---

### Task 4: Explicit transactional enqueue and the real `activity.completed` vertical slice

**Files:**
- Create: `linuxlab/sync/enqueue.py`
- Create: `linuxlab/sync/tests/test_task14_enqueue.py`
- Modify: `linuxlab/learning/services.py`
- Modify: `linuxlab/sync/registry.py`

**Interfaces:**
- Produces:
  - `enqueue_sync_event(domain_event, *, node_id, sync_enabled) -> OutboxMessage | None`
- Registers exactly one real existing event: `activity.completed`. No other event becomes syncable in Task 14.

- [ ] **Step 1: Inspect the real `activity.completed` DomainEvent payload before writing tests**

Record exact existing fields used by both production call sites. The Task 14 payload must be a minimal learner/result-safe subset already available at event publication time. Do not add answer keys, grading secrets, local database PKs, or invented fields.

- [ ] **Step 2: Write RED tests for sync-disabled, configuration error, and atomicity**

Sync-disabled: activity completion still succeeds, DomainEvent exists, OutboxMessage does not. Sync-enabled without Node ID: raises `SyncConfigurationError`. Business rollback rolls back DomainEvent and Outbox together. Exactly one Outbox row for one sync-enabled event.

- [ ] **Step 3: Write RED envelope snapshot test**

After enqueue, mutate/reload unrelated current domain state. The stored Outbox payload must remain exactly the original event snapshot.

- [ ] **Step 4: Run RED**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_enqueue.py -v
```

- [ ] **Step 5: Implement explicit enqueue**

`enqueue_sync_event()`: if sync disabled, return `None`; if event is unregistered, return `None`; if sync enabled and Node ID missing/invalid, raise `SyncConfigurationError`; resolve exact registry contract; serialize once; create one immutable `OutboxMessage` with a newly generated immutable `event_id`; remain inside caller's business transaction. Do not use Django signals or post-commit scanning.

- [ ] **Step 6: Wire both existing `activity.completed` publication paths**

Modify the existing business/application transaction so it explicitly performs the mutation, records `DomainEvent`, and passes that exact event into `enqueue_sync_event()`. Do not make every `publish_event()` globally syncable.

- [ ] **Step 7: Run GREEN plus learning regressions**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_enqueue.py linuxlab/learning/tests -v
```

- [ ] **Step 8: Commit**

```bash
git add linuxlab/sync/enqueue.py linuxlab/sync/registry.py linuxlab/sync/tests/test_task14_enqueue.py linuxlab/learning/services.py
git commit -m "feat: enqueue activity completion for sync"
```

---

### Task 5: Outbox claim, lease recovery, retry, and controlled delivery state

**Files:**
- Create: `linuxlab/sync/outbox.py`
- Create: `linuxlab/sync/tests/test_task14_outbox.py`

**Interfaces:**
- Produces: `claim_outbox_batch(...)`, `mark_delivered(event_ids, now=None)`, `mark_dead_letter(event_id, code, reason, now=None)`, `release_transient(event_ids, error_kind, now=None)`, `release_auth_failure(event_ids, now=None)`, `compute_retry_delay(attempts, *, base, cap, random_source)`.

- [ ] **Step 1: Write RED state-machine tests**

Only due `PENDING` rows are claimable; order is `next_attempt_at`, then `created_at`, then local PK; claim changes selected rows to `IN_FLIGHT` with `claimed_at`/`lease_until`; network is not invoked inside claim; `accepted`/`duplicate` may become `DELIVERED`; permanent rejection becomes `DEAD_LETTER`; timeout/5xx/missing ACK returns to `PENDING`; 401/403 returns to retryable state and never dead-letters; attempts increment on failed delivery attempts; transient failures are not dead-lettered at any finite attempt count.

- [ ] **Step 2: Write real PostgreSQL multi-connection claim test**

Use transaction-capable tests to prove two concurrent workers using `select_for_update(skip_locked=True)` cannot claim the same Outbox row. Do not mock away PostgreSQL locking.

- [ ] **Step 3: Write lease-expiry recovery test**

A row left `IN_FLIGHT` past `lease_until` must become reclaimable without changing immutable envelope/event ID.

- [ ] **Step 4: Write backoff/jitter tests**

Inject deterministic random source. Verify delay grows exponentially, jitter changes the exact delay within bound, cap is respected, no finite-attempt terminal failure.

- [ ] **Step 5: Write count + bytes batching tests**

The claimed/sent batch must stop when either max event count would be exceeded, or adding the next canonical serialized envelope would exceed byte budget. Oversized rows that do not fit the current batch must remain eligible and must not be silently marked delivered/dead-lettered.

- [ ] **Step 6: Run RED**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_outbox.py -v
```

- [ ] **Step 7: Implement minimal Outbox state service**

Use short `transaction.atomic()` blocks and `select_for_update(skip_locked=True)` for claims. Never keep the transaction open across transport/network calls. Only private/controlled write helpers may mutate delivery metadata.

- [ ] **Step 8: Run GREEN**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_outbox.py -v
```

- [ ] **Step 9: Commit**

```bash
git add linuxlab/sync/outbox.py linuxlab/sync/tests/test_task14_outbox.py
git commit -m "feat: add leased outbox delivery state"
```

---

### Task 6: `SyncTransport`, production HTTPS client, and non-production file transport

**Files:**
- Modify: `linuxlab/sync/contracts.py`
- Create/modify: `linuxlab/sync/transports.py`
- Create: `linuxlab/sync/tests/test_task14_transports.py`
- Modify: settings modules only for explicit Task 14 sync configuration

**Interfaces:**
- Produces: `SyncTransport.send_batch(events) -> BatchAck`, `HttpSyncTransport`, `FileSyncTransport`.
- `HttpSyncTransport` is production. `FileSyncTransport` is dev/test/reference and returns no production delivery confirmation.

- [ ] **Step 1: Write RED HTTPS policy tests**

Production `HttpSyncTransport` rejects `http://`; `https://` uses certificate/hostname verification; explicit dev/test insecure HTTP switch works only when intentionally supplied; there is no "HTTPS failed, retry HTTP" path; bearer secret is sent in `Authorization` and never included in body/log-safe repr; Node ID is supplied as immutable identity metadata.

- [ ] **Step 2: Write RED ACK parser tests**

Verify exact per-event mapping; unknown/malformed status does not become success; missing event result remains unconfirmed; truncated/malformed JSON produces transient/unconfirmed transport outcome; generic HTTP 2xx alone never marks every event delivered.

- [ ] **Step 3: Write RED HTTP failure classification tests**

Classify timeout/DNS/connect/5xx as transient; 401/403 as auth/channel fault; 400/413 as request/protocol failure; successful valid batch as per-event ACK. Do not convert 401/403 into individual dead letters.

- [ ] **Step 4: Write RED `FileSyncTransport` tests**

Writes deterministic canonical event batch file for dev/test/reference; contains no bearer secret; returns **unconfirmed** results for every event; a caller cannot infer `DELIVERED` solely from successful file write.

- [ ] **Step 5: Run RED**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_transports.py -v
```

- [ ] **Step 6: Implement transports with standard library**

Use Python standard-library HTTPS facilities with default certificate verification. Node runtime settings should be explicit and fail closed: `LINUXLAB_SYNC_ENABLED`, `LINUXLAB_NODE_ID`, `LINUXLAB_SYNC_SECRET`, `LINUXLAB_CENTRAL_URL`, `LINUXLAB_SYNC_ALLOW_INSECURE_HTTP` (false except explicit dev/test), `LINUXLAB_SYNC_BATCH_MAX_EVENTS`, `LINUXLAB_SYNC_BATCH_MAX_BYTES`, `LINUXLAB_SYNC_LEASE_SECONDS`, `LINUXLAB_SYNC_BACKOFF_BASE_SECONDS`, `LINUXLAB_SYNC_BACKOFF_CAP_SECONDS`. The secret comes from environment/local secret configuration, not DB.

- [ ] **Step 7: Run GREEN and settings checks**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_transports.py -v
.venv/bin/ruff check linuxlab/sync/transports.py
```

- [ ] **Step 8: Commit**

```bash
git add linuxlab/sync/contracts.py linuxlab/sync/transports.py linuxlab/sync/tests/test_task14_transports.py linuxlab/settings
git commit -m "feat: add Task 14 sync transports"
```

---

### Task 7: Durable Central Inbox ingestion and idempotency conflict detection

**Files:**
- Create: `linuxlab/sync/ingestion.py`
- Create: `linuxlab/sync/tests/test_task14_ingestion.py`

**Interfaces:**
- Produces: `ingest_event(authenticated_node, envelope) -> EventAck`, exact duplicate detection, `IDEMPOTENCY_CONFLICT` permanent rejection.

- [ ] **Step 1: Write RED ingestion validation tests**

`source_node_id != authenticated_node.node_id` → permanent reject; unknown event type → `UNSUPPORTED_EVENT`; unsupported schema → `UNSUPPORTED_SCHEMA`; invalid registered payload → `INVALID_PAYLOAD`; invalid events do not create Inbox rows.

- [ ] **Step 2: Write RED durable acceptance test**

After `ingest_event()` returns `accepted`, a committed `InboxMessage` must exist. Do not return accepted before durable insert.

- [ ] **Step 3: Write RED exact duplicate test**

Send the exact normalized envelope twice: first → `accepted`; second → `duplicate`; one Inbox row.

- [ ] **Step 4: Write RED idempotency-conflict test**

Reuse same `event_id` but modify one immutable field/payload: `rejected`; code `IDEMPOTENCY_CONFLICT`; original Inbox row unchanged.

- [ ] **Step 5: Write real concurrent duplicate test**

With separate DB connections, concurrently ingest the same new `event_id`. One durable row; outcomes normalize to one accepted + one duplicate (or equivalent race-safe semantics); no uncaught integrity error to API layer.

- [ ] **Step 6: Run RED**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_ingestion.py -v
```

- [ ] **Step 7: Implement event-level transaction**

Each event gets its own short transaction/savepoint boundary. On unique race: re-read existing Inbox row; compare canonical immutable event content; exact same → duplicate; different → idempotency conflict. Never overwrite the existing row.

- [ ] **Step 8: Run GREEN**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_ingestion.py -v
```

- [ ] **Step 9: Commit**

```bash
git add linuxlab/sync/ingestion.py linuxlab/sync/tests/test_task14_ingestion.py
git commit -m "feat: add idempotent Central inbox ingestion"
```

---

### Task 8: Authenticated batch API with request-level vs event-level semantics

**Files:**
- Create: `linuxlab/sync/api.py`
- Modify: `linuxlab/sync/urls.py`
- Create: `linuxlab/sync/tests/test_task14_api.py`

**Interfaces:**
- Produces: `POST /api/v1/sync/events/`. Consumes: `authenticate_node`, registry validation, `ingest_event`.

- [ ] **Step 1: Write RED route/auth tests**

Missing/malformed bearer → 401; unknown/wrong secret → 401; disabled/revoked registration → 403; authenticated Node continues. Do not expose whether a Node ID exists through detailed auth error bodies.

- [ ] **Step 2: Write RED request-protocol tests**

Malformed JSON/batch → 400; oversized count/bytes → 413; batch must be structurally valid before event-level ingest begins; API never accepts plaintext credential in JSON body.

- [ ] **Step 3: Write RED mixed-batch test**

One request includes one valid new event, one exact duplicate, one invalid payload/unsupported event. Expected 2xx request with independent results (`accepted`/`duplicate`/`rejected`). The rejected event must not roll back the accepted event.

- [ ] **Step 4: Write RED source spoof test**

Authenticate Node A and send an event claiming Node B. Expected permanent event rejection and no Inbox row for the spoofed event.

- [ ] **Step 5: Run RED**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_api.py -v
```

- [ ] **Step 6: Implement thin API adapter**

Parses Node ID metadata + Bearer header; authenticates once; validates batch structure/size; parses each event into `WireEventEnvelope`; delegates each event independently to `ingest_event`; serializes `EventAck` results. No Central learning-domain mutation belongs in this endpoint.

- [ ] **Step 7: Run GREEN plus existing URL tests**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_api.py linuxlab/sync/tests/test_views.py -v
```

Confirm Task 13 `/learn/<uuid>/` route remains unchanged.

- [ ] **Step 8: Commit**

```bash
git add linuxlab/sync/api.py linuxlab/sync/urls.py linuxlab/sync/tests/test_task14_api.py
git commit -m "feat: add authenticated sync ingestion API"
```

---

### Task 9: Processor contract without Central learning projection

**Files:**
- Create: `linuxlab/sync/processing.py`
- Create: `linuxlab/sync/tests/test_task14_processing.py`

**Interfaces:**
- Produces: `SyncEventProcessor` protocol/interface, controlled state transitions `RECEIVED -> PROCESSED | FAILED`. Does not mutate `Attempt`, `Result`, or `Progress`.

- [ ] **Step 1: Write RED processing-state tests**

Accepted Inbox begins `RECEIVED`; a successful test processor marks `PROCESSED` and `processed_at`; failed processor marks `FAILED` and stores safe error metadata; processing failure leaves Inbox event envelope unchanged; processing failure has no effect on Node ACK/delivery semantics.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_processing.py -v
```

- [ ] **Step 3: Implement minimal processor boundary**

Define a protocol and one service that runs a caller-supplied processor against an Inbox event and updates only processing metadata. Do not create a processor that writes Central learning aggregates in Task 14.

- [ ] **Step 4: Run GREEN**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_processing.py -v
```

- [ ] **Step 5: Commit**

```bash
git add linuxlab/sync/processing.py linuxlab/sync/tests/test_task14_processing.py
git commit -m "feat: add Central sync processor boundary"
```

---

### Task 10: `flush_outbox()` orchestration and dedicated Node sync worker

**Files:**
- Modify: `linuxlab/sync/services.py` only for the authoritative public `flush_outbox()` façade if required
- Create/modify: `linuxlab/sync/outbox.py`
- Create: `linuxlab/sync/management/commands/sync_events.py`
- Create: `linuxlab/sync/tests/test_task14_flush.py`
- Create: `linuxlab/sync/tests/test_task14_command.py`

**Interfaces:**
- Produces: `flush_outbox(transport, ...) -> FlushSummary`, `manage.py sync_events --once`, continuous mode suitable for external supervision. `flush_outbox()` owns no business mutation and no Central projection.

- [ ] **Step 1: Write RED successful flush test**

Arrange several due PENDING rows. Transport returns event A accepted, event B duplicate. Both → DELIVERED; delivered timestamps set; immutable envelope untouched.

- [ ] **Step 2: Write RED partial/missing ACK test**

Transport sends A/B/C but returns ACK only for A/B. A/B transition according to ACK; C returns to retryable PENDING with backoff; C is not inferred delivered.

- [ ] **Step 3: Write RED permanent-rejection test**

One event is `rejected` with permanent code. Only that event → DEAD_LETTER; good neighbors still delivered.

- [ ] **Step 4: Write RED timeout/5xx test**

All claimed events return to PENDING; attempts increment; next attempt gets backoff+jitter; no dead-letter despite large attempt count.

- [ ] **Step 5: Write RED 401/403 channel-fault test**

All claimed events remain retryable; none dead-letter; flush result indicates auth/degraded channel; command uses long backoff/pause behavior.

- [ ] **Step 6: Write RED FileSyncTransport non-delivery test**

A successful file export must leave events unconfirmed/retryable, never DELIVERED.

- [ ] **Step 7: Run RED**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_flush.py linuxlab/sync/tests/test_task14_command.py -v
```

- [ ] **Step 8: Implement `flush_outbox()`**

Claim bounded batch in short DB transaction; commit claim; call transport outside transaction; validate per-event results; update delivery metadata only for confirmed event IDs; release unconfirmed/transient/auth-fault events safely. Do not reserialize from current domain state.

- [ ] **Step 9: Implement `sync_events` command**

Support `--once` for deterministic testing/admin; continuous loop for service-manager use; explicit sleep/backoff; graceful Ctrl-C/termination; no background thread in Django web process. Build `HttpSyncTransport` from Node-local settings/env in production.

- [ ] **Step 10: Run GREEN**

```bash
.venv/bin/pytest linuxlab/sync/tests/test_task14_flush.py linuxlab/sync/tests/test_task14_command.py -v
```

- [ ] **Step 11: Commit**

```bash
git add linuxlab/sync/services.py linuxlab/sync/outbox.py linuxlab/sync/management/commands/sync_events.py linuxlab/sync/tests/test_task14_flush.py linuxlab/sync/tests/test_task14_command.py
git commit -m "feat: add Node outbox sync worker"
```

---

### Task 11: End-to-end vertical slice and failure-recovery proofs

**Files:**
- Create: `linuxlab/sync/tests/test_task14_e2e.py`
- Modify only production code where a test exposes a genuine spec defect

**Interfaces:**
- Proves the complete real path: actual learning `activity.completed` → DomainEvent → OutboxMessage → worker/transport → Central auth → InboxMessage → ACK → Outbox DELIVERED.

- [ ] **Step 1: Write full real vertical-slice test**

Use the real Task 13/learning domain fixture and actual `activity.completed` production service. Prove: sync enabled + valid Node ID; activity completion records DomainEvent + Outbox in same transaction; flush sends exact stored envelope; Central authenticates Node; Central writes one Inbox row; ACK accepted; Node marks Outbox delivered.

- [ ] **Step 2: Add commit-then-ACK-lost replay test**

Central durable Inbox insert; response loss before Node sees ACK; Node retries same event ID; Central returns duplicate; one Inbox row only; Node eventually DELIVERED.

- [ ] **Step 3: Add poison-neighbor test**

Batch has good event + permanent invalid event + good event. Both good events accepted/delivered independently; poison event dead-letters.

- [ ] **Step 4: Add long-offline recovery test**

Repeated transient failures do not delete/dead-letter the event. Later successful Central response delivers the same event ID/payload.

- [ ] **Step 5: Add processor-failure-after-ACK test**

After Inbox acceptance and Node delivery, run a failing Central test processor. Node remains DELIVERED; Inbox remains durable; processing state FAILED; no Node retransmission required.

- [ ] **Step 6: Add no-package/no-Central→Node regression assertions**

Task 14 must not download `.llpkg`, activate packages, mutate Task 12 InstalledPackage, or create Central→Node event transport.

- [ ] **Step 7: Run E2E and related regression suites**

```bash
.venv/bin/pytest \
  linuxlab/sync/tests/test_task14_e2e.py \
  linuxlab/learning/tests \
  linuxlab/runtime/tests \
  linuxlab/publication/tests -v
```

- [ ] **Step 8: Commit**

```bash
git add linuxlab/sync/tests/test_task14_e2e.py
git commit -m "test: prove Task 14 event sync end to end"
```

---

### Task 12: Documentation record, security audit, and full completion gate

**Files:**
- Modify: `docs/devlog/2026-08-30-task14-sync-decisions.md`
- Optionally update spec only with actual implementation filenames; no semantic change without human approval

**Interfaces:**
- Produces final Task 14 implementation report for independent review.

- [ ] **Step 1: Audit secrets and trust boundaries**

Confirm: no plaintext Central credential persistence; no credential/hash API response or logging; no grading material in the `activity.completed` sync payload; no Task 11/12 package workflow mutation.

- [ ] **Step 2: Audit transport semantics**

Confirm: HttpSyncTransport is production HTTPS-only; FileSyncTransport never causes production DELIVERED; no generic 2xx whole-batch delivery inference; missing ACK IDs remain retryable; 401/403 is channel fault; all network calls occur after DB claim transaction commits.

- [ ] **Step 3: Audit event immutability/idempotency**

Confirm: Outbox envelope update paths are blocked; Inbox envelope update paths are blocked; exact duplicate accepted; same-ID/different-content rejected without overwrite; no latest/global ordering behavior.

- [ ] **Step 4: Append implementation record to dev log**

Record commit range; final model and module names; migration number; selected real `activity.completed` vertical slice and exact payload fields; credential config names; HTTP route; tests for transaction rollback, concurrency, ACK loss, duplicate/conflict, auth, lease recovery, offline recovery, and FileSyncTransport non-delivery; no semantic changes to T14-D01–D39.

If implementation requires a new architectural decision, STOP and obtain human approval rather than inventing T14-D40.

- [ ] **Step 5: Run focused Task 14 sync suite**

```bash
.venv/bin/pytest linuxlab/sync/tests -v
```

- [ ] **Step 6: Run learning/runtime/package regressions**

```bash
.venv/bin/pytest \
  linuxlab/learning/tests \
  linuxlab/runtime/tests \
  linuxlab/publication/tests \
  linuxlab/activities/tests \
  linuxlab/assessment/tests \
  linuxlab/quiz/tests \
  linuxlab/catalog/tests \
  linuxlab/content/tests -v
```

- [ ] **Step 7: Run complete suite**

```bash
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest -v
```

Expected: all 566 baseline tests plus Task 14 tests pass.

- [ ] **Step 8: Run Ruff and Django migration/system checks**

```bash
.venv/bin/ruff check .
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py check
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py makemigrations --check --dry-run
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py check
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py makemigrations --check --dry-run
```

Expected: no system-check issues; no migration drift beyond the checked-in additive Task 14 migration.

- [ ] **Step 9: Scope audit**

```bash
git status --short
git diff abae15b..HEAD --stat
git diff abae15b..HEAD --name-only
```

Confirm absent: Central → Node event transport; package auto-download; Central learning projections; Celery/Redis/RabbitMQ/Kafka; DB replication; self-service enrollment; automatic GC.

- [ ] **Step 10: Commit documentation**

```bash
git add docs/devlog/2026-08-30-task14-sync-decisions.md
git commit -m "docs: record Task 14 sync implementation"
```

- [ ] **Step 11: Produce final implementation report and STOP**

Do not start the next task. Independent Task 14 architecture/security review is mandatory before closure.
