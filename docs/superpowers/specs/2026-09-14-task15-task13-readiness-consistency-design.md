# Task 15 / Task 13 Corrective B — Readiness Consistency Design

Date: 2026-09-14
Status: Binding transcription of the human-approved Corrective B design
Repository: LinuxLab
Branch: `feature/phase1-learning-foundation`
Baseline: `cd040cefc62f34f30840361577d4454d47d0bdac`

Amended after human review from `d4573c4ddbd83211323a8340e5b6e3d548958569`:
logical-node-only external identity, dedicated read-only probe retained,
Outbox-only requeue, and conservative legacy retry-age migration.

## 1. Authority, context, and scope

Corrective A solved the Phase-1 package operator-surface blocker and is closed
at the baseline above. Corrective B establishes runtime/code consistency.
Corrective C follows B and reconciles operator-facing documentation,
checklists, and descriptive contract tests. This document specifies B; it is
neither implementation nor an implementation plan. Existing behavior cited
below is evidence of the starting point, not a substitute for approved B
behavior.

Architecture authorities inspected:

- `docs/architecture/LinuxLab-Architecture-Spec-v1.md`, especially sections
  4–6, 8, 11–12, 15, 17–19, 21–23.
- Accepted ADR-001, ADR-002, ADR-007, ADR-008, ADR-010, ADR-011, and ADR-012:
  Django/Wagtail, modular monolith, independent PostgreSQL deployments,
  offline profiles, package/event transport, model ownership and UUIDs,
  and guest-first identity.
- `docs/plans/Phase1-Learning-Foundation.md`.
- `docs/superpowers/specs/2026-09-11-task15-task13-package-operator-surface-design.md`,
  including Amendments A–C and the A → B → C review sequence.
- `docs/superpowers/plans/2026-09-11-task15-task13-package-operator-surface-implementation-plan.md`,
  particularly its interface inventory, trust boundary, and verification scope.

One Django/Wagtail codebase and independently owned PostgreSQL databases
remain mandatory. No application database replication or registration-table
copying is introduced. Guest progress stays local by default. Immutable
Activity versions, published artifacts, package identities, server-side
answer secrecy, and existing module ownership remain intact.

B includes health/readiness runtime semantics; canonical, profile/capability-aware
local, remote, and release diagnostics; an operator diagnostic command;
an explicit read-only Central probe; release-gate consumption; registration
lifecycle, rotation, revoke, data migration and DB invariant; semantic
transport outcomes; persisted retries, jitter, budget and operator attention;
explicit requeue; typed deployment configuration; and the code, tests and
migrations directly needed to establish those behaviors in later work.

B excludes operator-manual reconciliation, env-file documentation corrections,
libpq installation prose, checklist paths, release invocation documentation,
HTTP/backoff prose reconciliation, Wagtail/admin diagnostics UI, dashboards,
monitoring/Prometheus/metrics infrastructure, Celery, RabbitMQ, Kafka,
Kubernetes, microservices, a generic distributed scheduler, generic policy
engine/DSL, automatic remediation/self-healing, automatic registration
provisioning, per-event retry policy, trust/signing or multi-key trust redesign,
package operator redesign, FA-M1 cleanup, Task-7 M2 cleanup, Corrective C,
and Task14. Existing files named `test_task14_*` and existing sync machinery
are context for B's bounded corrections; this does not start or close Task14.

## 2. Repository reconciliation and concrete names

| Surface | Baseline evidence | Binding B name/behavior |
|---|---|---|
| Liveness | `linuxlab/core/health.py:healthz`, `linuxlab/urls.py` register `/healthz` | `/health/` uses the same liveness semantics; retain `/healthz` as a direct compatibility alias |
| Readiness | `readyz`, `evaluate_readiness`, `ReadinessResult`; `/readyz` also checks sync configuration | `/ready/` is local readiness; retain `/readyz` as a direct alias to the same evaluator |
| Diagnostics | No canonical diagnostic report or command | `linuxlab.core.diagnostics` public contracts; `manage.py diagnose_deployment` |
| Release gate | `scripts/phase1_release_gate.py`; `scripts.release_gate.gate_runner.run_release_gate`; `processes.wait_ready` polls `/readyz` | Keep these entry points; consume canonical RELEASE diagnostics in the readiness/capability decision |
| Registration | `linuxlab.sync.models.NodeRegistration`; unique `node_id`; `enabled`, `revoked_at`; mutable current/previous secret hashes | Preserve logical `node_id`; add `state`; historical rows retain local DB identity, at most one ACTIVE |
| Registration services | `linuxlab.sync.authentication.issue_node_registration`, `rotate_node_credential`, `revoke_node_registration`, `authenticate_node` | Keep names and ownership; replace in-place/grace authentication with the approved lifecycle |
| Delivery authority | `linuxlab.sync.models.OutboxMessage`; `attempts`, `next_attempt_at`, claim/lease/token fields | Reuse this record and these fields; add failure-cycle metadata, not another queue |
| Delivery states | `OUTBOX_PENDING`, `OUTBOX_IN_FLIGHT`, `OUTBOX_DELIVERED`, `OUTBOX_DEAD_LETTER` | Reuse `pending`, `in_flight`, `delivered`, `dead_letter`; dead letter means operator attention |
| Worker | `linuxlab.sync.outbox.claim_outbox_batch`, `flush_outbox`; `manage.py sync_events [--once]` | Preserve worker entry points, immutable envelopes, leases and fencing; apply B eligibility and retry rules |
| Transport | `SyncTransport`, `HttpSyncTransport`, `FileSyncTransport`, `BatchAck`, `EventAck` | Preserve boundaries and ACK authority; classify outcomes before retry policy |

The approved `/health/` and `/ready/` paths are concrete URLs, not redirects.
Compatibility aliases prevent existing deployment probes from breaking and
must never diverge in semantics. Existing lower-case readiness reason codes
remain internal; they are not an established canonical diagnostic-code
contract. The eight upper-case diagnostic codes in section 4 are adopted
exactly.

At baseline, `NodeRegistration` is Central-owned and Node reads its provisioned
`LINUXLAB_NODE_ID` and `LINUXLAB_SYNC_SECRET` from local deployment settings.
There is no Node-local authoritative registration mirror. B retains that
boundary: a local Node can inspect credential configuration, but only Central
can prove current registration authority. Section 5 makes this evidence limit
explicit rather than inventing synchronized registration state.

At baseline, rotation overwrites hashes and accepts the previous secret during
`grace_period`; authentication can select one row because `node_id` is unique.
B deliberately replaces those mechanics. Likewise, baseline retries are
unlimited, auth failures return immediately to pending without increment, and
HTTP 400 is bisected then delayed by twice the cap. These are the precise
behaviors B corrects, not compatibility guarantees to retain.

## 3. Hybrid health and readiness

`/health/` and `/healthz` return HTTP 200 with `{"status":"ok"}` when the process
can answer. They perform no DB, key, registration, sync or network checks.

`/ready/` and `/readyz` return HTTP 200 with `{"status":"ok"}` when the deployment
can serve local traffic, otherwise HTTP 503 with `{"status":"not_ready"}`.
The common evaluator checks recognized deployment profile, required local
profile configuration (including a nonempty database name), DB connectivity,
and compatible applied migration/schema state. It must not execute migrations
or write any data. Retain the existing internal `test` profile for test
harnesses; production diagnostic reports require `central` or `node`.

Readiness uses core's shared local DB/schema primitives. It is not the overall
result of deep diagnostics. Remove sync-only configuration as a local-readiness
prerequisite. Signing, trust, credentials, registration, retries, Central
connectivity and Central authentication belong to capability diagnostics and
release policy. A missing signing/trust key must not prevent serving already
installed valid local content.

Healthy Node local prerequisites plus Internet loss or unavailable Central
means readiness remains successful, including when a release policy requires
a Central probe. Malformed sync-only policy also leaves healthy local
liveness/readiness successful. HTTP bodies and logs must not expose exception
text, settings, secrets or internal diagnostic details.

## 4. Canonical diagnostics contract and ownership

`core` owns `DiagnosticCheck`, `DiagnosticContext`, `DiagnosticReport`, the
provider protocol, aggregation, report validation and common local checks in
`linuxlab.core.diagnostics`. A provider receives the profile, mode and typed
capability context and returns its owned check results. Providers participate
through explicit registration/interfaces at application composition; core
must not import or query publication/sync internals. Provider registration is
in-memory and must perform no DB or network work at application startup.

`publication` owns its diagnostic provider and `SIGNING_KEY`, using the existing
`load_package_signing_private_key()` and `PackageSigningConfigurationError`.
`sync` owns its provider, `TRUST_KEY`, `NODE_REGISTRATION`, `SYNC_RETRY_POLICY`,
`CENTRAL_CONNECTIVITY`, `CENTRAL_AUTH_ACCEPTANCE`, and its release requirements.
`TRUST_KEY` uses `load_package_trusted_public_key()` and
`PackageTrustConfigurationError`. The providers live within their owning apps;
commands and release scripts cannot reimplement the checks.

Schema version 1 has this shape (the example is a valid partial LOCAL report,
not the required complete report for every capability):

```json
{
  "schema_version": 1,
  "profile": "node",
  "mode": "local",
  "overall_status": "PASS",
  "checks": [
    {
      "code": "DB_CONNECTIVITY",
      "status": "PASS",
      "message": "Local database is reachable.",
      "details": {}
    }
  ]
}
```

`schema_version` is integer 1, not a boolean. `profile` is `central|node`;
`mode` is `local|remote|release`. Each check has one unique stable code, one
`PASS|WARN|FAIL` status and safe human message. `details` is optional and must
be a safe bounded JSON object. No `SKIPPED` or fourth status is introduced.
Checks are ordered by stable code for deterministic serialization; repeated
codes, invalid types or statuses are invalid reports. Human `message` text
never drives machine decisions.

Canonical aggregation: any FAIL gives FAIL; otherwise any WARN gives WARN;
otherwise PASS. Adapters must not recalculate or replace `overall_status`.
Core's canonical validator rejects inconsistencies; adapters call that
validator rather than implementing another aggregation algorithm. Provider
failure never silently removes a required check: emit that owned check as
FAIL with sanitized failure information, or fail diagnostic execution if a
valid report cannot be produced.

| Stable code | Owner | Meaning |
|---|---|---|
| `DB_CONNECTIVITY` | core | Local configured PostgreSQL can be reached |
| `SCHEMA_STATE` | core | Profile/local configuration is valid and schema is compatible with this code; inspect migration state only |
| `SIGNING_KEY` | publication | Required Central signing key can be read and parsed as Ed25519 |
| `TRUST_KEY` | sync | Required Node pinned trust key can be read and parsed as Ed25519 |
| `NODE_REGISTRATION` | sync | Registration authority or local credential evidence, explicitly distinguished by profile in section 5 |
| `SYNC_RETRY_POLICY` | sync | Required sync deployment configuration is typed and usable, including release probe policy when applicable |
| `CENTRAL_CONNECTIVITY` | sync | Explicit probe can reach the configured Central endpoint |
| `CENTRAL_AUTH_ACCEPTANCE` | sync | That endpoint accepts the provisioned Node credential under ACTIVE-only authority |

If DB connectivity fails, `SCHEMA_STATE` is FAIL because compatibility cannot
be established; it must not be reported PASS or silently omitted. No speculative
checks, metrics or generic policy engine are added.

Diagnostics are observational in every mode. They must not create/rotate/revoke
registrations, provision credentials, retry/requeue sync, change counters,
rewrite settings, generate keys, repair schema/data, emit persistent audit
records for the act of diagnosis, or auto-heal. Tests must prove absence of
persistent DB/file mutations on success and failure, on both sides of a probe.
Remediation is always a separate explicit service/command action.

The secret boundary applies to stdout, stderr, reports, details, logs and
persisted error metadata: no bearer tokens, Authorization headers, private
signing keys, private trust material, DB passwords or raw sensitive response
bodies. Providers construct allow-listed output rather than sanitizing model
or settings dumps. Messages are limited to 512 characters; details to 4096
UTF-8 JSON bytes with only check-specific safe scalar fields (booleans, counts,
HTTP status and stable error codes). Registration inventory reports aggregate
counts, never unbounded row lists. The complete report is limited to 65536
UTF-8 bytes. Over-limit or unknown detail fields fail canonical validation.
Signing/trust checks retain A's fixed paths:
`/etc/linuxlab/keys/package-signing.key` on Central and
`/etc/linuxlab/keys/package-signing-public.key` on Node. No private key ships
to Node; no diagnostic creates, rewrites or changes trust material.

## 5. Profile, capability and mode selection

Use existing capability flags (`LINUXLAB_CAN_AUTHOR`,
`LINUXLAB_CAN_IMPORT_PACKAGES`, `LINUXLAB_CAN_RECEIVE_SYNC_EVENTS`,
`LINUXLAB_CAN_RUN_SYNC_WORKER`) and `sync.capabilities` helpers for authority.
`LINUXLAB_SYNC_ENABLED` remains the explicit Node opt-in to outbound sync.
A profile label alone must not confer capability.

The canonical selection matrix, used by both diagnostics and its validator,
is:

| Check | Central LOCAL/RELEASE | Node LOCAL/RELEASE |
|---|---|---|
| DB_CONNECTIVITY, SCHEMA_STATE | Required | Required |
| SIGNING_KEY | Required when authoring enabled | Absent |
| TRUST_KEY | Absent | Required when package import enabled |
| NODE_REGISTRATION | Required when receiving sync enabled | Required when outbound sync enabled or a remote probe is requested/required |
| SYNC_RETRY_POLICY | Absent; Node release policy is inapplicable | Required when outbound sync enabled or a remote probe is requested/required; invalid Node probe-policy configuration also emits FAIL |
| CENTRAL_CONNECTIVITY, CENTRAL_AUTH_ACCEPTANCE | Absent | Absent in LOCAL; required in REMOTE; required in RELEASE only by deployment policy |

On Central, `NODE_REGISTRATION` inspects each existing logical node in its
owned registration table. Exactly one usable ACTIVE per logical node is PASS;
zero ACTIVE, multiple ACTIVE, or structurally unusable ACTIVE is FAIL. An empty
Central registration inventory is PASS with a bounded zero count: there is no
logical node whose registration is required yet. Historical SUPERSEDED/REVOKED
rows alongside a valid ACTIVE create no warning. A history-only logical node
has zero ACTIVE and fails, as does an explicitly revoked node with no replacement.
A structurally usable ACTIVE has a valid logical node UUID
and a usable supported Django password hash; this does not prove possession
of the secret.

On Node, `NODE_REGISTRATION` inspects the locally provisioned UUID and nonblank
secret, not a copied Central table. Missing/malformed credential configuration
is FAIL. Structurally valid local configuration yields WARN with
`details.authority_verified=false`: current Central ACTIVE authority is
unverified locally. It must not claim authoritative ACTIVE/PASS from mere
configuration. In REMOTE or required RELEASE probing, successful
`CENTRAL_AUTH_ACCEPTANCE` supplies the separate authority evidence; the local
check's meaning is unchanged. Rejection makes the remote check FAIL. This
profile distinction preserves both offline diagnostics and Central ownership.

`SYNC_RETRY_POLICY` validates the policy and worker prerequisites when sync
is enabled. URL validity preserves `HttpSyncTransport`'s HTTPS requirement and
makes the configuration boundary explicit: valid host, no
embedded username/password or fragment; insecure HTTP is accepted only under
the existing explicit development/test setting. A disabled worker need not
be enabled merely to make a read-only remote probe, but probe credentials and
URL must still be valid. Invalid configuration prevents the dependent action
before claims or network I/O.

LOCAL is the default, deterministic local-check mode with zero Central calls.
Remote checks not selected are absent. REMOTE is explicit local checks plus
the two remote checks; it is Node-only and performs no remediation. Central
REMOTE invocation is rejected with a sanitized nonzero command error before
network work. RELEASE derives required checks from canonical deployment
configuration, never from an operator override. Required missing checks fail
release even if the supplied overall status claims PASS.

Final command interface:

```text
python manage.py diagnose_deployment [--mode local|remote|release] [--format text|json]
```

Defaults are `--mode local --format text`. `--mode remote` is the explicit
remote request. `--mode release --format json` is the release adapter contract.
Normal Django `--settings` selects the deployment; no diagnostic `--profile`
spoof, `--skip-check`, `--no-remote`, `--force`, policy override or remediation
flag exists. Invalid flag combinations fail. JSON stdout contains exactly one
report, with no Django system-check banners or traceback contamination.
Expected diagnostic FAIL still produces its valid JSON report. PASS and WARN
exit 0; FAIL exits 1; invalid invocation or an unrecoverable command crash
exits nonzero. Text is a rendering of the same report/status, not another
source of truth.

## 6. Typed deployment configuration

Canonical Django deployment configuration is the sole policy source; the DB
is not a policy store and release scripts must not parse environment strings
independently. Retain the repository's existing base/cap names instead of
creating competing aliases. The exact settings and optional defaults are:

| Setting | Approved semantic mapping | Typed value/default | Valid range |
|---|---|---|---|
| `LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE` | Same approved name | bool, `false` | Explicit true/false |
| `LINUXLAB_SYNC_BACKOFF_BASE_SECONDS` | `LINUXLAB_SYNC_RETRY_BASE_DELAY` | seconds, `5.0` | Finite positive number |
| `LINUXLAB_SYNC_BACKOFF_CAP_SECONDS` | `LINUXLAB_SYNC_RETRY_MAX_DELAY` | seconds, `3600.0` | Finite positive number, strictly greater than base |
| `LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS` | Same approved name | integer, `10` | 1 through 2147483647 |
| `LINUXLAB_SYNC_RETRY_MAX_AGE` | Same approved name | seconds, `86400.0` | Finite positive number |

The approved base/delay names in the mapping column are semantic references,
not additional accepted settings. Base strictly below cap preserves existing
validation. Durations must also be representable by the runtime's duration
and timestamp arithmetic; overflow is invalid configuration. Missing optional
values receive the stated defaults; empty strings are malformed, not missing.
Numeric booleans, NaN/infinity, nonintegral attempt counts, zero/negative values
and invalid cross-field bounds are rejected. Environment booleans accept only
case-insensitive `true`/`false` after surrounding whitespace removal; arbitrary
strings must not silently become false. Programmatic settings use their typed
counterparts.

`linuxlab.sync.configuration` owns a typed `SyncConfigurationError` and a typed
`SyncRetryPolicy`. The shared settings boundary yields valid typed values or
a typed deferred configuration failure; it must not raise during Django
settings import solely because sync policy is malformed. Every worker, probe
and diagnostic provider obtains configuration through the same public sync
configuration interface. Existing `BackoffConfigurationError` callers must be
reconciled to this single error/policy authority, not given a second parser.

Invalid required policy means `SYNC_RETRY_POLICY` FAIL, no sync action, and
RELEASE FAIL when applicable. A valid disabled Node does not require worker
credentials/retry capability for local release. Invalid Node release-probe
policy cannot be treated as false: its diagnostic FAIL blocks release. Central
does not read or apply the Node-only release-probe policy. Invalid sync-only
configuration must not break healthy `/health/` or `/ready/` through eager
settings evaluation, including existing sync-only numeric settings read along
that import path. No silent fallback is allowed at capability use.

## 7. Remote Central probe

Use a dedicated read-only `GET /api/v1/sync/probe/` endpoint in the existing
sync API, guarded by `can_receive_sync_events()` and the same
`authenticate_node` service as event ingestion. This small probe does not
send empty or real event batches through the ingestion endpoint. It creates
no InboxMessage, OutboxMessage, DomainEvent, registration or session state.
Its successful response is HTTP 200 `{"status":"ok"}`; failed authentication
uses the existing generic safe authentication response with 401/403. A Node
profile refuses Central probe service authority.

The Node probe uses the configured Central base URL, existing provisioned
secret and `X-LinuxLab-Node-Id` header, with bearer authorization only in its
header. Use the existing urllib transport stack, HTTPS certificate validation,
a 30-second timeout and no automatic retry. Do not follow redirects carrying
credentials; a redirect is a failed probe. Bound the probe body to 4096 bytes
and require the documented success shape before claiming authentication
acceptance. Never log or persist its raw body or headers.

For a valid endpoint response, connectivity is PASS. Valid authenticated 200
is auth PASS. Reachable 401/403 is connectivity PASS and auth FAIL. Other HTTP
responses, malformed success bodies or unexpected redirects cannot prove
authentication and give auth FAIL. Network, DNS, timeout or TLS failure gives
connectivity FAIL and auth FAIL (acceptance could not be established). Missing
local probe prerequisites gives both remote checks FAIL without a network
call. A report never drops a required check because another one failed.

Outside RELEASE only explicit REMOTE requests call Central. In RELEASE, Node
policy false means neither remote check runs or is required; true means both
run and are required. No manual flag can disable this production requirement.
The probe never changes Node readiness, retry state or credential lifecycle.

## 8. Thin release-gate consumption

Keep `.venv/bin/python scripts/phase1_release_gate.py [--keep-on-failure]` as the
existing gate entry point and `run_release_gate` as orchestrator. The diagnostic
consumption path invokes `diagnose_deployment --mode release --format json`
in the target profile's own process/environment and validates through the
public canonical core report validator. Sync/publication supply applicability
requirements through their interfaces; scripts must not duplicate policy.

The required code set is derived from the same canonical configuration/context
used for the deployment being checked, not trusted from an unverified report
or independently interpreted shell variables. The adapter validates process
exit, exact supported diagnostic schema, expected profile, `mode=release`,
required codes, unique codes, all status values, report structure and canonical
status consistency. Nonzero exit, command timeout/crash, malformed JSON,
unsupported schema, profile/mode mismatch, invalid status, duplicate/missing
required code, or overall FAIL fails closed. A supported valid report with
required PASS/WARN and overall WARN passes this diagnostic gate. It must not
parse human messages or recompute overall status.

Diagnostic report schema version 1 is distinct from the existing release
artifact's schema version 1. Do not replace the established evidence schema,
resource ownership, cleanup authority or exit-code algebra merely because the
version numbers coincide. A diagnostic startup/capability failure uses the
existing STARTUP_FAILURE classification (exit 3); command-line/preflight
configuration failure before deployment setup remains PRECHECK_FAILURE (2).
Existing scenario and cleanup failures still independently fail the full gate.

The current `_run_manage_command` intentionally uses the `postgres` maintenance
DB for model checks. It is not a valid diagnostic invocation context: DB/schema
and registration diagnostics must target the actual deployment DB and actual
provisioned configuration. Keep model/drift checks separate from diagnostics.
`processes.wait_ready` may retain `/readyz` through its compatibility alias;
an HTTP readiness success alone is no longer evidence of deep release capability.

The diagnostic/release capability decision must not inspect key/trust internals,
query NodeRegistration directly, map HTTP outcomes, independently probe Central,
retry outbox rows, rotate credentials, provision or repair state. Existing
isolated E2E fixture/scenario actions remain separate explicit tests of domain
behavior, not diagnostic remediation. Their setup must not be invoked by the
diagnostic command or used to turn failed deployment diagnostics into PASS.
Production diagnostics never import release-gate ephemeral key machinery or
accept alternate trust paths. Tests substitute key boundaries within their
isolated fixtures only; they must not modify production `/etc/linuxlab` files.

## 9. Registration identity, authority and lifecycle

`sync.NodeRegistration` remains Central-owned. `node_id` is the logical node
UUID used by `source_node_id` and authentication headers; rotation preserves
it. Historical registration rows use local DB identity plus lifecycle state;
no additional public/distributed identifier is introduced. Operator commands
address the logical node by `node_id`; a local PK must never become an external
or operator selector. Remove unconditional uniqueness of `node_id`, retaining
an index.
Historical `InboxMessage.source_node` foreign keys continue pointing to the
registration originally used; rows and local PKs are not rewritten or exposed
as distributed identity. Existing canonical envelope reconstruction through
`source_node.node_id` remains stable across rotation.

Use `NodeRegistration.state` with `REGISTRATION_ACTIVE="active"`,
`REGISTRATION_SUPERSEDED="superseded"`, and
`REGISTRATION_REVOKED="revoked"`. These mean ACTIVE, SUPERSEDED and REVOKED
respectively. Add a DB allowed-state check and a PostgreSQL partial unique
constraint on `node_id` where `state="active"`, named
`unique_active_node_registration_per_node`. No permanent AMBIGUOUS state exists.

ACTIVE is the sole runtime authority. SUPERSEDED is legitimate rotation
history and never authenticates. REVOKED is explicitly invalidated and never
authenticates or resurrects. Runtime resolves exactly one `(node_id,
state=ACTIVE)` and checks its current hash. Zero or multiple ACTIVE fails
closed; multiple is an invariant failure, never a heuristic selection.
No `latest()`, newest timestamp, highest PK/version, arbitrary `first()`, or
`enabled` plus ordering may select authority. Ordering solely to acquire locks
is not current-registration selection.

Retain `current_secret_hash` as the per-registration credential hash. Runtime
must not accept `previous_secret_hash` or `previous_valid_until`. Legacy
`enabled` is replaced by state as authority; legacy previous-hash/grace fields
and lifecycle columns retained during migration are historical only, never a
second auth source. Retain these historical columns in B, frozen against new
credential/enable writes; do not add a second authority or a cleanup migration
merely to remove history. New records leave previous-secret/grace values empty.

`authenticate_node` retains generic `NodeAuthenticationError` failure semantics.
The API adapter must delegate lifecycle interpretation to sync's auth service;
an old `enabled=False` query must not remain an independent authority. No
registration state is sent to learner browsers or inferred from event payloads.

## 10. Explicit registration operations and serialization

All registration writes stay in `linuxlab.sync.authentication` through the
model-owned controlled writer/insertion boundary. The fixed mutable-field
allow-list must permit lifecycle state changes but must no longer permit
in-place changes to a historical credential hash. Ordinary ORM writes remain
blocked. Each mutating service checks Central receive-sync capability itself.

Serialize issue-for-existing-node, rotation and revoke within
`transaction.atomic()` using a PostgreSQL transaction-scoped advisory lock on
the logical node UUID in a registration-specific namespace, followed by a fresh
ACTIVE resolution. This covers a scope with zero rows, which row locking alone
cannot protect. Hash collisions may only serialize unrelated scopes, never
merge authority. Keep the partial uniqueness constraint as the final DB
invariant. No logical-node lock table or distributed lock subsystem is added.

`issue_node_registration(display_name, *, node_id=None)` remains explicit
administration: omitted `node_id` issues a new logical UUID; an explicitly
supplied UUID provisions a new registration only when that scope has zero
ACTIVE. Existing ACTIVE or multiple ACTIVE fails without mutation. This is the
bounded explicit remediation after revocation or ambiguous migration; it never
reactivates a historical row and never runs automatically.

`rotate_node_credential(node_id)` requires exactly one usable ACTIVE A. Generate
a new registration row with a fresh high-entropy bearer secret; mark A
SUPERSEDED and insert B as ACTIVE in the same locked transaction. Physically
supersede A before inserting ACTIVE B to satisfy immediate partial uniqueness;
credential preparation in memory is not runtime authority. Any insertion or
commit failure rolls back A's transition. No committed state exposes two ACTIVE.
Do not create B as SUPERSEDED and then resurrect it. Only B's new credential
may authenticate after commit; A's secret stops immediately.

The previous `grace_period` service argument and `--grace-hours` CLI option are
removed; passing them is an explicit error, never a silently accepted grace
window. `manage_sync_node rotate <node_uuid>` delegates to the new contract.
Concurrent rotations serialize and re-resolve current authority: a later
successful rotation may supersede the preceding rotation's result, but never
creates two ACTIVE or reuses a historical secret.

`revoke_node_registration(node_id)` changes exactly one ACTIVE to REVOKED
transactionally, retaining `revoked_at` as evidence. Zero ACTIVE fails safely
without creating a replacement; multiple ACTIVE is an invariant error. It
never rotates, re-enables, provisions Central state or copies a credential.
After revoke, NODE_REGISTRATION fails for that logical node until an explicit
new registration is issued. REVOKED and SUPERSEDED records are retained.

Keep `manage_sync_node issue --name NAME`, adding optional `--node-id UUID` for
explicit replacement in a zero-ACTIVE scope; retain `revoke <node_uuid>`.
`IssuedNodeCredential` continues returning the logical node ID and one-time
plaintext secret only, and excludes secret from repr. Only explicit issue/rotate
may display a new secret once,
as already authorized by their existing command contract. Diagnostics never do.

## 11. Deterministic registration migration

The baseline has no `revoked` boolean. Its exact legacy revoked predicate is
`enabled=False OR revoked_at IS NOT NULL`; revocation evidence wins over a
contradictory enabled flag. Non-revoked means `enabled=True AND revoked_at IS
NULL`. This is the mapping of the approved abstract `revoked=true/false` rules
to current schema, not an assumption that a nonexistent field exists.

Schema support introduces lifecycle state without premature ACTIVE defaults.
Retain existing local row identities and logical `node_id` values; no identity
backfill or new distributed registration identifier is introduced. Group by
`node_id`:

| Legacy group | Result |
|---|---|
| Revoked rows | Each REVOKED, regardless of other rows |
| Exactly one non-revoked candidate | That candidate ACTIVE |
| Zero non-revoked candidates | No ACTIVE; migration completes; diagnostics require explicit remediation |
| Two or more non-revoked candidates | All candidates REVOKED; zero ACTIVE; explicit remediation required |

No timestamp, PK ordering, token age or version selects an ACTIVE candidate.
The baseline unique `node_id` makes multiple candidates impossible in an intact
baseline DB, but the data migration still handles them after schema expansion;
actual migration tests must seed that intermediate historical state. Local
PKs remain private row identities and never select current authority or become
operator selectors.

Preserve historical rows, hashes, timestamps and Inbox foreign keys. Do not
create credentials, rotate secrets, contact Central, provision replacements or
introduce AMBIGUOUS. Normal old previous-secret fields confer no post-migration
grace authority. A sole non-revoked but unusable hash remains ACTIVE by the
specified data mapping; the capability/auth checks fail closed and require
explicit remediation rather than silently creating a credential.

Mandatory migration dependency order is lifecycle schema support (including
relaxing logical-node uniqueness), deterministic data migration, explicit
verification of at most one ACTIVE per logical node, then the partial unique
and allowed-state constraints. Avoid a default that grants all old rows ACTIVE
before classification. Use historical migration models, not current runtime
services. No reverse migration may guess lost enabled/grace authority or pick
a current row; make the semantic data migration explicitly irreversible.
Stop old credential-writing processes during this local schema/code cutover;
old code must not run against the multi-registration schema. This is a local
upgrade boundary, not a requirement to upgrade every Node and Central together.

## 12. Transport/network outcome taxonomy

`linuxlab.sync.contracts` defines `SUCCESS`, `PERMANENT_REJECTION`,
`AUTH_OR_REGISTRATION_FAILURE`, `TRANSIENT_FAILURE`, and `RATE_LIMITED` as stable
semantic outcomes. The transport/network boundary maps concrete conditions
once and supplies safe status/error metadata and parsed Retry-After where
relevant. Retry policy consumes outcomes; it must not inspect HTTP numbers.

| Concrete outcome | Semantic result |
|---|---|
| Successful 2xx | SUCCESS at request transport level; per-event ACK validation still required |
| 400 invalid payload/schema/request | PERMANENT_REJECTION |
| 401 or 403; invalid/revoked registration | AUTH_OR_REGISTRATION_FAILURE |
| 408 | TRANSIENT_FAILURE |
| Network/DNS/connection/TLS error or timeout | TRANSIENT_FAILURE |
| 5xx | TRANSIENT_FAILURE |
| 429 | RATE_LIMITED |
| Malformed/missing/ambiguous ACK evidence | TRANSIENT_FAILURE for each unconfirmed event |
| Valid per-event rejection with a rejection code | PERMANENT_REJECTION for that event |

Preserve `AuthTransportError`, `TransientTransportError`,
`ProtocolTransportError`, `RequestTooLargeError` as existing transport boundary
names where useful, carrying their semantic category; add a distinct
`RateLimitedTransportError` for RATE_LIMITED. HTTP 400 no longer triggers
bisection/retry or a special twice-cap singleton delay: all affected attempted
rows require operator attention. Do not implement “all 4xx are permanent.”
Other unclassified HTTP errors retain the conservative TRANSIENT_FAILURE
mapping and now have a finite retry budget. Credentials must never be
forwarded across redirects.

HTTP 413 retains the established bounded request-size bisection as a transport
adaptation, not an additional retry category/policy engine. Each failed send
counts once for every involved event; each smaller resend checks the budget
first. A singleton still rejected as oversized maps to PERMANENT_REJECTION
with `EVENT_TOO_LARGE`. Splitting must not bypass attempt or age limits.

Transport SUCCESS alone must never mark an Outbox row delivered. Preserve
`BatchAck`/`EventAck` semantics: only an unambiguous accepted/duplicate ACK for
that exact event UUID authorizes `delivered`. Missing/conflicting duplicate
ACK entries or a rejection without code remain unconfirmed and retryable
within budget. Do not infer delivery from a bare 2xx or a successful file
write. `FileSyncTransport` remains dev/test/reference only and returns
unconfirmed events. Immutable envelopes and idempotent Central ingestion are
unchanged; partial batch outcomes affect only their corresponding rows.

## 13. Authoritative persisted retry state

The only retry authority is the existing Node-local `OutboxMessage`. Reuse
`attempts` for approved `attempt_count`, `next_attempt_at` for due time, existing
`state`, claim/lease/token fields, delivery timestamp and rejection metadata.
Add nullable `first_failure_at`, `last_failure_category` (empty before any
failure, otherwise one of the failure categories), and optional nullable
`last_http_status` (included in B). Reuse `rejection_code` for bounded safe machine error code;
`rejection_reason` may retain only safe controlled human text, never a raw
remote reason/response. No parallel scheduler/delivery/retry table is added.

`attempts` means failed delivery attempts in the current retry cycle. It is
not claims, successful sends, worker loops or total lifetime deliveries. First
failed send sets attempts to 1 and `first_failure_at=now`; each later failed
send increments exactly once and preserves that first timestamp. Transport
failure affecting a batch counts once per attempted row. A preflight policy
failure with no send does not increment. Stale/fenced result writes affect
zero rows and must not increment counters. On success retain cycle evidence;
only explicit requeue resets it.

State mapping deliberately reuses the existing domain:

| Approved concept | Existing storage |
|---|---|
| PENDING | `OUTBOX_PENDING="pending"`, initially due |
| RETRY_WAIT | `pending` with persisted retry metadata and `next_attempt_at` |
| In-progress claim | `OUTBOX_IN_FLIGHT="in_flight"` with current lease/token |
| SUCCEEDED | `OUTBOX_DELIVERED="delivered"` |
| REQUIRES_OPERATOR_ATTENTION | `OUTBOX_DEAD_LETTER="dead_letter"` with failure evidence |

Do not add parallel `succeeded`, `retry_wait` or `requires_operator_attention`
states merely for vocabulary. `dead_letter` is terminal for automatic retry,
not permission to delete the event and not necessarily an assertion that its
payload is invalid. Auth faults and exhausted transient budgets use it too.

For existing Outbox rows, retain envelope, state, attempts, due time, lease and
claim tokens. Delivered/dead-letter rows remain terminal. The baseline records
no first-failure timestamp, so do not invent an exact history or reset retries
to zero. For retryable legacy rows with attempts > 0, conservatively seed
`first_failure_at=created_at`; document that this is an upper-bound age anchor,
not the actual first failure time. It intentionally grants no fresh retry-age
budget and must not use the migration-time timestamp. Rows with attempts=0
retain null first failure. Unknown historical category remains empty until the
next actual failure. At the first worker eligibility evaluation, already-exhausted
rows may enter operator attention without another send after upgrade;
`attempts > 0` alone does not unconditionally dead-letter a row. Apply the configured
attempt/age budget. This migration is fail-closed and deterministic, not silent
retry-budget renewal.

## 14. Retry budget, jitter, and Retry-After

After a failed delivery, persist the increment and first-failure timestamp.
PERMANENT_REJECTION and AUTH_OR_REGISTRATION_FAILURE immediately transition to
`dead_letter`; no automatic retry is scheduled. TRANSIENT_FAILURE and
RATE_LIMITED may return to pending only when both budgets remain:

```text
attempts < max_attempts
AND now - first_failure_at < max_retry_age
```

Equality at either boundary exhausts the budget. Either
`attempts >= max_attempts` OR `now-first_failure_at >= max_retry_age` transitions
to operator attention without another automatic send. Age is measured using
timezone-aware UTC instants and an injected clock seam in tests. Check budget
before each automatic send as well as after failure, so downtime or a long
schedule cannot authorize a send beyond the age boundary.

Full-jitter exponential backoff for retryable failure is:

```text
n = max(attempts - 1, 0)
cap_n = min(max_delay, base_delay * 2^n)
delay = uniform duration in [0, cap_n]
```

Thus the first failure's maximum delay is base_delay, not twice base_delay.
Production uses real randomness; tests inject clock and randomness through
the existing `compute_retry_delay` seam. Compute the cap without overflow for
large attempt counts. Zero and upper-bound jitter are valid test cases; jitter
must not be removed or replaced by fixed production delays.

For RATE_LIMITED/429, parse Retry-After at the transport boundary. Support
integer delta-seconds and HTTP-date with the standard HTTP date parser.
Malformed/nonfinite/unparseable values use full jitter. A negative delta or
already expired HTTP-date gives a zero-delay candidate. Date differences use
the receiving Node's UTC clock; do not persist or echo the raw header. Valid
Retry-After is preferred over jitter, and delay is capped at max_delay.

Neither a zero candidate nor a long Retry-After bypasses either budget. Set
next due time to the earlier of `now+delay` and the retry-age deadline so the
worker can terminalize at the deadline; reaching that time authorizes a budget
check, not a send if the budget has expired. An exhausted failure never schedules
another automatic attempt. Terminal rows clear active claim/lease/token state;
`next_attempt_at` may retain its last due timestamp as inert evidence since
state excludes selection.

## 15. Worker eligibility, fencing and explicit requeue

Retain `claim_outbox_batch` short PostgreSQL transactions with
`select_for_update(skip_locked=True)`, bounded batch selection, and network I/O
outside transactions. Future due rows, delivered rows, dead-letter rows and
unexpired in-flight leases are excluded. RETRY_WAIT becomes send-eligible only
when `next_attempt_at <= now` and both budgets remain. Exhausted eligible
candidates transition to dead letter without transport. Expired in-flight
leases may be reclaimed only subject to the same budget and a fresh claim token.
A crash with no recorded outcome is not fabricated as a known failed attempt;
replay remains safe through the existing immutable UUID/idempotent ACK contract.

Every outcome write is conditional on IN_FLIGHT and the exact claim token;
retain `_write_fenced`/model-owned allow-list protections. Durable scheduling
survives process restart. Restart, credential repair and configuration changes
must not auto-requeue dead-letter rows. The existing worker process remains
explicitly supervised; no web-process background thread or broker is added.

Explicit operator adapter: `manage.py sync_events requeue --event-id UUID`.
Existing `sync_events` and `sync_events --once` remain the worker forms; requeue
is mutually exclusive with worker execution. Requeue is Node-capability-gated
and calls `linuxlab.sync.outbox.requeue_outbox_message(event_id)`; it never
sends the event itself. There is no bulk-all or automatic reset mode.

Requeue locks the exact dead-letter row, validates required typed configuration,
and fails without mutation for unknown, pending, in-flight or delivered rows.
In one transaction it sets state pending, attempts 0, first_failure_at null,
next_attempt_at now, and clears claim/lease/token and current-cycle failure
fields. The only persistent mutation is this OutboxMessage state transition;
a failed transition rolls back completely.

Repository audit-pattern decision: `NO_EXISTING_PATTERN_EVENT_REMOVED`.
`linuxlab/core/events.py:publish_event` is a generic DomainEvent creation helper,
not an operator-remediation audit contract. Its production callers in
`linuxlab/learning/services.py` emit `activity.completed` for learning results.
`linuxlab/sync/authentication.py:revoke_node_registration` and
`linuxlab/sync/outbox.py:mark_dead_letter` use controlled model writers without
publishing an operator audit event. Existing command reporting uses stdout/stderr
(`manage_sync_node.Command.handle`, `sync_events.Command._report`). None
establishes a standardized operator-remediation DomainEvent pattern.

Preserve the existing safe command-reporting/logging mechanisms and any evidence
already retained through them; do not create a new persistent audit record or
a domain-event contract for requeue. Retain the original immutable Outbox
envelope, event UUID and existing domain-event trace link. Resetting current-cycle
fields does not delete that trace or any pre-existing domain history. Requeue
must not publish a DomainEvent, create another outbound event, cause event
recursion, or introduce a new distributed contract. Do not edit another app's
rows or build a workflow/audit subsystem. Diagnostics never call requeue.
Operator remediation can repair credentials/configuration first; remote proof of repair is not a
new mandatory online dependency for the explicit local reset action.

## 16. Required behavioral verification for later implementation

Tests in B establish the behavior directly needed here; this spec-only task
creates or modifies no test. Old assertions requiring grace authentication,
unlimited retries, immediate auth retry, singleton-400 backoff or sync-gated
local readiness must be replaced by these approved assertions, not retained
as contradictory acceptance criteria. Unrelated operator prose/checklist
reconciliation remains C.

### Diagnostics and adapters

- PASS-only → overall PASS/exit 0; PASS+WARN → WARN/exit 0; any FAIL → FAIL/nonzero.
- Exact schema/profile/mode, unique stable codes, allowed statuses and bounded
  safe details; human-message changes do not change any machine decision.
- LOCAL performs zero Central calls; unselected remote checks absent; required
  absent checks fail RELEASE. A provider exception cannot hide required work.
- Expected failures produce parseable JSON; malformed report/JSON, unsupported
  schema (including boolean version), wrong profile/mode, duplicate/missing
  code, invalid status, inconsistent overall, timeout/crash and nonzero process
  exit all fail the release consumer closed. WARN-only passes.
- Success/failure diagnostics make zero persistent mutation, proved by complete
  relevant row/field and file comparisons, not merely equal row counts. Probe
  checks prove this on Central and Node. No remediation service is invoked.
- Ownership tests/review ensure core and adapters consume public providers,
  never query foreign internals or independently parse release policy.

### Registration and migration

- Rotation creates a distinct local registration row and new secret; A becomes
  SUPERSEDED, B ACTIVE, same logical UUID, exactly one ACTIVE. Old and previous
  secrets fail immediately; no grace argument is silently honored.
- Revoke yields REVOKED with no replacement; zero ACTIVE fails auth/diagnostics.
  Explicit issue after revoke creates a new record, never resurrects history.
- ACTIVE-only auth; zero/multiple ACTIVE fail without ordering heuristics;
  unusable ACTIVE fails; healthy lifecycle history produces no warning.
- Real PostgreSQL transaction/concurrency tests for concurrent rotation,
  rotation versus revoke/issue, rollback on failed insertion, model write
  guards and the one-ACTIVE DB constraint. Use an intentionally unconstrained
  historical fixture to test multiple-ACTIVE defense; production uniqueness
  must not be bypassed merely to make ordinary runtime fixtures convenient.
- Execute actual historical migrations: legacy revoked, exactly one candidate,
  zero candidate and 2+ candidates. Vary insertion order, timestamps and PKs
  to prove no current-row heuristic; every ambiguous group has zero ACTIVE.
- Preserve logical node UUIDs, local row identities and Inbox foreign keys;
  no new registration identity or external PK selector. Prove deterministic
  lifecycle migration, irreversible semantics and post-migration rejection of
  two ACTIVE.
  No migration contacts Central, generates credentials or performs remediation.

### Retry and delivery

- 400 → permanent/operator attention/no retry; 401/403 and invalid/revoked
  registration → auth failure/operator attention/no retry.
- 408, network/timeout and 5xx → transient retry while budget remains.
- 429 valid delta-seconds and HTTP-date preferred over jitter; malformed header
  uses jitter; negative/expired candidate zero; excessive value capped.
- First failure count 1, exactly one increment per failed send, stable
  first_failure_at, success retains evidence, stale-token results cannot count.
- Attempt boundary below/at max, age below/at max, either budget ending first,
  no extra send when age expires between scheduling and claim, and a delay
  crossing the deadline. Retry-After cannot bypass either budget.
- Deterministic jitter zero/upper endpoints, exponent on first/subsequent
  failure, cap and overflow behavior, with real production randomness retained.
- Future due rows not selected; terminal rows never auto-selected; restart
  preserves persisted schedule/count/age; expired leases retain fencing.
- Explicit requeue creates a new retry cycle through an atomic Outbox-only
  transition, preserves event UUID/envelope and existing domain-event trace,
  refuses wrong states and never sends immediately. Prove zero new DomainEvent
  or outbound event, no event recursion, and rollback on transition failure.
- Actual retry-state migration preserves old due times/counts/terminal states.
  Retryable legacy rows with attempts > 0 use first_failure_at=created_at;
  attempts=0 keeps NULL. Prove no migration-time budget renewal, exhausted rows
  cannot send, and nonexhausted rows are not unconditionally dead-lettered.
  The anchor is an age upper bound, never a claim of actual first failure time.
- 2xx with missing/malformed/duplicate ACK is not delivery; partial ACKs remain
  per-event; repeated accepted event UUID has one Central effect. Bounded 413
  splits honor counters/budgets and singleton EVENT_TOO_LARGE remains terminal.

### Offline, configuration, release and secrecy

- Healthy Node + Central unavailable + require-probe=false: `/ready/` and alias
  succeed; LOCAL makes zero Central calls; RELEASE can pass required local
  checks (WARN for locally unverified configured credentials is allowed).
- Same Node + require-probe=true: readiness still succeeds; connectivity FAIL,
  release diagnostic FAIL and gate FAIL. Reachable rejected auth yields
  CENTRAL_AUTH_ACCEPTANCE FAIL. Explicit REMOTE cannot change RELEASE policy.
- Disabled standalone sync does not require credentials or a live Central;
  Central never applies the Node-only probe policy.
- For every new typed setting and retained base/cap setting: missing optional
  value has its specified default, valid value is typed, malformed and
  out-of-range values produce a typed capability failure. Test cross-field
  ranges, boolean traps, empty values, nonfinite durations and overflow.
- Malformed required retry policy gives SYNC_RETRY_POLICY FAIL and no worker
  claim/send; malformed sync-only values do not crash settings import or healthy
  offline liveness/readiness. Invalid release-policy text never becomes false.
- Test-only token/private-key/password/Authorization/raw-body sentinels are
  absent from stdout, stderr, reports, logs, stored errors and audit records.
  URL credentials, redirects and malformed probe responses fail safely.

Later B verification includes narrow failing/passing tests, related core/sync/
publication/runtime tests, scripts/release-gate tests, real PostgreSQL
concurrency and migration tests, the full repository pytest suite, `ruff check .`,
Central and Node `manage.py check`, and both profiles' migration drift checks.
Results must be reported from actual execution; this document makes no claim
that these future behavior tests already pass at the baseline.

## 17. Conceptual rollout dependencies

The dependency order is diagnostics foundation → typed configuration →
NodeRegistration lifecycle schema → deterministic registration data migration
→ one-ACTIVE DB invariant → registration services/runtime lookup → sync outcome
taxonomy → persisted retry state → module diagnostic providers → operator
diagnostic adapter → remote probe → release-gate integration → integration,
concurrency and migration verification → full verification.

This is dependency ordering only, not a task-by-task implementation plan.
Registration schema/data/invariant and service cutover are one coordinated
local upgrade boundary. Node remains independently deployable/offline; no
Central–Node DB replication or continuously connected rollout is introduced.

## 18. Corrective C boundary

Corrective C happens AFTER B behavior is established and reviewed. B establishes
behavior; C describes and reconciles it. C will address previously identified
wrong env/documentation references, missing release-gate invocation, readiness
prose overstating diagnostics, registration revoke/rotation remediation docs,
HTTP 400/backoff docs, libpq/install dependencies, checklist JSON path and other
Task13 operator-doc/test reconciliation. B does not edit those manuals or
checklists. It does not start Corrective C or Task14, or declare Task13 closed.

## 19. Binding acceptance invariants

- Node local readiness is offline-first and independent of Central availability.
- Diagnostics are read-only; remediation is explicit and separate.
- Release gate consumes canonical diagnostics and deployment policy.
- Machines use stable codes/statuses, never human message prose; adapters never
  recalculate overall status.
- Module ownership and one-codebase deployment profiles are preserved.
- `node_id` is the sole external registration selector; historical rows use
  local DB identity plus lifecycle state, with no external/operator PK selector.
- One logical node has at most one ACTIVE registration; REVOKED never resurrects.
- Rotation creates a new ACTIVE and supersedes the old ACTIVE.
- Current registration is never inferred by latest, PK, timestamp or version.
- Ambiguous legacy registrations receive zero ACTIVE authority.
- Node-local credential evidence does not masquerade as Central authority.
- HTTP/network classification is separate from retry policy; ACK authority is
  preserved and no bare 2xx implies event delivery.
- Retry state is persisted and survives restart on the authoritative Outbox row.
- Exponential full jitter is used, including the first-failure exponent zero.
- Retry-After cannot bypass retry budget; max_attempts OR max_retry_age ends it.
- Permanent/auth failures do not auto-retry; operator attention requires explicit
  requeue, which is an atomic Outbox-only transition retaining existing history
  and trace evidence without a new DomainEvent or outbound event.
- Malformed sync config fails the required sync capability closed; malformed
  sync-only configuration alone does not destroy healthy offline local readiness.
- Diagnostics, reports, logs and persisted retry errors contain no secrets.
- No database replication, distributed subsystem, automatic self-healing,
  registration auto-provisioning or generic per-event policy framework is added.
- Package trust remains the fixed local single-key model established by A.
- This work adds only this specification; implementation, C and Task14 remain
  separate authorized work.

## 20. Self-review conclusion

The concrete names above distinguish existing interfaces from B additions.
Registration migration translates actual enabled/revoked_at fields, preserves
logical node UUID, local row identity and Inbox history, introduces no additional
distributed registration identity, and grants no authority by ordering. Rotation
has no hidden grace compatibility. Retry reuses existing Outbox state and
fencing, defines failed-attempt counting, safe legacy age handling, finite
budget and Retry-After boundaries without weakening ACK semantics. Local
readiness is independent of diagnostics and sync-only configuration. Release
requirements and aggregation have one canonical source. Secret handling,
read-only diagnosis and explicit remediation remain distinct. Requeue follows
the verified absence of an operator-remediation event pattern: Outbox-only
mutation, existing safe reporting, no new DomainEvent or outbound event. Legacy
retry-age migration retains the deterministic created_at upper-bound anchor,
never a fresh migration-time budget. No architecture invariant requires an ADR
change; no operator documentation, Corrective C or
Task14 work is included.

Open Questions: None
