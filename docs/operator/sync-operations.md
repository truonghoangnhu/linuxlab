# Sync operations

Node → Central sync moves completed learner activity events from a Node's
local Outbox to Central's durable Inbox over `HTTPS POST
/api/v1/sync/events/`, authenticated with a bearer credential Central
issued to that Node. Content/configuration only ever flows the other way
(Central → Node, as signed `.llpkg` packages) — this document covers the
learner-event direction only.

Never rewrite `OutboxMessage` or `InboxMessage` rows directly with SQL to
"fix" a stuck sync state. Every state transition is meant to happen
through the real ingestion/worker code paths below, because those paths
enforce the idempotency and trust guarantees (unique event IDs, hashed
credential checks, atomic claim/lease) that a manual SQL edit would
bypass.

## Node provisioning

A Node must be provisioned by Central before it can sync. There is no
self-service enrollment. On Central:

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py manage_sync_node issue --name "Lab A"
```

This prints the new Node's UUID and a plaintext bearer secret **exactly
once**. Copy both into the Node's `node.env` as `LINUXLAB_NODE_ID` and
`LINUXLAB_SYNC_SECRET` immediately — Central stores only a password hash
of the secret and cannot show it again.

## Credential rotate / revoke

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py manage_sync_node rotate <node_uuid> --grace-hours 24
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py manage_sync_node revoke <node_uuid>
```

`rotate` issues a new secret (again printed once) while keeping the
previous secret valid for `--grace-hours` (default 24), so a Node that has
not yet picked up the new secret does not lose sync mid-rotation. Update
the Node's `LINUXLAB_SYNC_SECRET` before the grace period expires.

`revoke` disables the Node's current **and** previous secret immediately.
A revoked Node's sync attempts fail authentication (HTTP 401/403); its
local learning continues uninterrupted (see "Central unreachable" below),
and its Outbox stays pending until a new credential is issued and applied.

## Running the worker

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py sync_events --once   # single flush cycle
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py sync_events           # continuous loop, for service-manager use
```

The worker never runs as a background thread inside the Gunicorn web
process — it is a separate, explicitly supervised process. No reference
systemd unit ships for it yet (`deploy/systemd/linuxlab-node.service` only
runs Gunicorn); supervise it with your own service definition matching
your host's conventions. `--once` is useful for a manual check or a
cron-style invocation; the no-flag continuous loop is meant for a
long-running supervised service and backs off after an auth or protocol
fault before retrying.

## Central unreachable / Node offline

If Central cannot be reached, learners are unaffected — Node grades and
records attempts locally regardless of sync state, and the Outbox simply
accumulates pending events. No manual intervention is required; the
worker resumes normal delivery once Central is reachable again with a
valid, non-revoked credential.

## Diagnosing pending / dead-letter state

There is no dedicated inspection command or admin screen for
`OutboxMessage`/`InboxMessage` in Phase 1. Inspect them read-only through
the Django shell:

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py shell -c \
  "from linuxlab.sync.models import OutboxMessage; \
   print(list(OutboxMessage.objects.filter(state='pending').values('event_id','attempts','next_attempt_at')))"
```

Read methods (`filter`, `get`, `values`, ...) are always safe; the
underlying model deliberately blocks any direct `save()` / `update()` /
`delete()` outside its own service functions, so there is no way to "fix"
a row this way even by mistake.

Outbox states: `pending` → `in_flight` (claimed, leased) → `delivered`, or
`dead_letter` for a permanent rejection (unsupported event type/schema
version, invalid payload, or a 413 payload-too-large that survives batch
bisection). A transient failure (timeout, 5xx, lost ACK) never
dead-letters — it always returns to `pending` with an increased backoff
delay.

## HTTP 400 vs 413

The sync endpoint distinguishes:

- **413** — the request body exceeds `LINUXLAB_SYNC_BATCH_MAX_BYTES`, or
  the event count in the batch exceeds `LINUXLAB_SYNC_BATCH_MAX_EVENTS`.
  The worker bisects the batch and retries the surviving events
  separately; a single event that is itself oversized is dead-lettered as
  `EVENT_TOO_LARGE` and does not block its neighbors.
- **400** — the request body is not valid JSON, or does not match the
  expected envelope shape. This is a request-level parse failure — the
  whole batch is rejected, retried with a long, non-configurable backoff
  (since a malformed batch is a protocol-level problem, not an ordinary
  transient failure), and never dead-lettered on its own.
- **401/403** — the bearer credential is missing, unknown, wrong, or
  belongs to a revoked/disabled registration. This is a channel fault, not
  a payload defect: affected events stay pending and are never
  dead-lettered. Resolve it by reissuing/rotating the Node's credential
  (see above) and updating `node.env`.

## Retry / backoff

Ordinary transient failures (timeout, 5xx, unconfirmed ACK) use
exponential backoff with jitter, bounded by two settings (Node-only,
consumed in `linuxlab/sync/outbox.py`):

```
LINUXLAB_SYNC_BACKOFF_BASE_SECONDS=5      # default
LINUXLAB_SYNC_BACKOFF_CAP_SECONDS=3600    # default; must be greater than the base
```

401/403 auth faults and the HTTP 400 protocol-fault path use their own,
separate, non-configurable timing — see [configuration.md](configuration.md)
for the full settings reference.
