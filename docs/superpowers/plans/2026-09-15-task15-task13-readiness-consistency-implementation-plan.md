# Task 15 / Task 13 Corrective B — Readiness Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the approved offline-first readiness, canonical diagnostics, registration lifecycle and bounded persistent sync retry behavior.

**Architecture:** Keep one Django/Wagtail modular monolith with independent Central and Node PostgreSQL databases. Module-owned diagnostic providers expose a core-owned report contract, and release adapters consume its canonical validation. Registration authority stays in sync services and retry state stays on OutboxMessage.

**Tech Stack:** Django 5.2, PostgreSQL, pytest, Python 3.12, existing LinuxLab modular-monolith infrastructure.

**Spec:** `docs/superpowers/specs/2026-09-14-task15-task13-readiness-consistency-design.md`

## Global Constraints

- One Django/Wagtail codebase; independent Central/Node PostgreSQL databases; no DB replication or registration-table copying.
- Node is offline-first. `/health/` is process liveness only; `/ready/` is local readiness only. Central availability does not determine local readiness. Keep `/healthz` and `/readyz` as direct aliases.
- Diagnostics are read-only. Remediation is explicit and separate. The dedicated `GET /api/v1/sync/probe/` has no Inbox, Outbox, DomainEvent, registration, session or retry mutation.
- Release gate consumes canonical diagnostics and deployment policy. Stable code/status drives machines, never human messages; adapters do not recalculate overall status.
- Preserve module ownership. Core cannot import/query sync or publication internals. Providers register through explicit interfaces with no startup DB/network work.
- No `NodeRegistration.public_id`, identity backfill or distributed registration identifier. `node_id` is the sole external/operator registration selector; local PKs stay private.
- At most one ACTIVE per logical node. No latest/PK/timestamp/version authority. REVOKED never resurrects; rotation inserts a new row and supersedes the old ACTIVE. Ambiguous migration grants zero ACTIVE authority.
- Transport taxonomy and retry policy stay separate. A bare 2xx never overrides per-event ACK authority. Immutable envelopes and event UUID idempotency remain intact.
- OutboxMessage remains retry authority: no second queue/scheduler/table or parallel state machine. Reuse `attempts`, `next_attempt_at`, pending/in_flight/delivered/dead_letter.
- Persist retry state across restart. Full jitter is required. Retry-After cannot bypass max_attempts OR max_retry_age. Permanent/auth failures stop automatically; dead_letter means operator attention.
- Explicit requeue is an atomic Outbox-only transition: no new DomainEvent, outbound event, recursion or audit subsystem. Preserve existing trace/history and safe reporting.
- Malformed sync config fails the dependent capability closed without silent fallback; malformed sync-only configuration cannot unnecessarily break healthy local readiness or settings import.
- Never expose bearer secrets, Authorization headers, private signing/trust material, DB passwords, raw sensitive responses or learner payloads through diagnostics, repr, logs or persisted failure fields. Only explicit issue/rotate may print a new secret once.
- Keep A's fixed signing/trust paths and single-key trust model. No private key ships to Node; production never imports release-harness key machinery.
- Preserve guest-first learning, anonymous progress local by default, exact immutable Activity versions, published package artifacts and server-side answer secrecy.
- No FA-M1 or Task-7 M2 cleanup. No Corrective C or Task14 work. Operator manuals, env documentation, libpq prose, checklist paths, release invocation prose, HTTP/backoff documentation and documentation-only contract tests are excluded.
- No Celery, RabbitMQ, Kafka, microservices, distributed scheduler, generic policy engine, automatic provisioning/self-healing, dashboards or admin diagnostics UI.

## Execution and Review Contract

Planning baseline: `6a1d17ff6a88d7729e8c621624143352ab7e5c98` on
`feature/phase1-learning-foundation`. This file is the only change made by
plan authoring. At execution, begin Task 1 from the reviewed plan commit;
subsequent tasks begin from the preceding cleared implementation commit.

Execution is **Codex implementer → Claude independent exact-range review →
ChatGPT adjudication → next task only after clearance**. No automatic continuation
between tasks. Record each task's start and end full SHA, changed files,
migrations, exact test counts, check results and concerns. Tasks 1–12 each end
in one primary commit with a clean tree. Task 13 is verification only: no edit
or commit. Review uses `git diff <start-sha>..<end-sha>` and the binding spec.

Every task begins with `git rev-parse HEAD`, `git branch --show-current`,
`git status --short`, and `git diff --check`; stop on unrelated work, do not
reset it. Each task's RED failure must be the named missing behavior, not a
broken PostgreSQL environment or unrelated import failure. Every additional boundary behavior repeats write → expected RED → minimal
implementation → GREEN using that task's focused command. After GREEN, refactor
only the touched behavior and rerun its focused suite. Never weaken a test to
preserve behavior the spec explicitly replaces.

The requested dependency order is retained with two testing clarifications:
Task 2 tests malformed sync values via settings overrides; fresh-process
malformed environment import is Task 3's parser boundary. Task 7 proves legacy
state preservation; Task 8 proves whether those migrated rows send or become
terminal, because selection policy does not exist in Task 7. Task 4 and Task 5
are separate review commits but one local deployment cutover; schema-only
Task 4 is not a deployable new-auth release. Stop old credential writers during
that cutover, as the spec requires.

## File Responsibility Map

Paths without “new” exist at the planning baseline. Symbol names are the stable
anchors; numeric line positions will move during earlier tasks. This map is
also the allowed implementation scope; broader test execution does not authorize
unrelated edits.

| Task | Production/support files | Test files and responsibility |
|---|---|---|
| 1 | New `linuxlab/core/diagnostics.py`: data contracts, registry, validation, aggregation | New `linuxlab/core/tests/test_diagnostics.py`: pure report contract |
| 2 | `linuxlab/core/health.py`: shared local primitives/evaluator; `linuxlab/urls.py`: aliases | `linuxlab/core/tests/test_health.py`: HTTP semantics |
| 3 | New `linuxlab/sync/configuration.py`: typed/deferred policy; `linuxlab/settings/base.py`: safe setting decoding; `linuxlab/sync/outbox.py`: delegate old backoff bounds; `linuxlab/sync/management/commands/sync_events.py`: worker preflight; `linuxlab/sync/api.py`: typed inbound limits | New `linuxlab/sync/tests/test_configuration.py`; `linuxlab/sync/tests/test_task15_backoff_settings.py`; `linuxlab/sync/tests/test_task14_command.py`; `linuxlab/core/tests/test_settings_profiles.py`: configuration/import boundary |
| 4 | `linuxlab/sync/models.py`: registration schema; new `linuxlab/sync/migrations/0005_registration_lifecycle_schema.py`, `0006_registration_lifecycle_data.py`, `0007_registration_lifecycle_constraints.py` | New `linuxlab/sync/tests/_migration_db.py`, `linuxlab/sync/tests/test_registration_migrations.py`; `linuxlab/sync/tests/test_task14_models.py`: historical migration/constraints |
| 5 | `linuxlab/sync/authentication.py`, `linuxlab/sync/models.py`, `linuxlab/sync/api.py`, `linuxlab/sync/management/commands/manage_sync_node.py`: lifecycle authority | New `linuxlab/sync/tests/test_registration_concurrency.py`; modify `linuxlab/sync/tests/test_task14_authentication.py`, `test_task14_models.py`, `test_task14_api.py`, `test_task14_profiles.py`, `test_task14_ingestion.py`, `test_task14_processing.py`, `test_task14_e2e.py` in that same directory: lifecycle and explicitly authorized fixture provisioning |
| 6 | `linuxlab/sync/contracts.py`, `linuxlab/sync/transports.py`: outcomes, Retry-After, safe HTTP | `linuxlab/sync/tests/test_task14_contracts.py`, `linuxlab/sync/tests/test_task14_transports.py`: transport/ACK contracts |
| 7 | `linuxlab/sync/models.py`; new `linuxlab/sync/migrations/0008_outbox_retry_state.py`, `0009_outbox_retry_age_data.py` | New `linuxlab/sync/tests/test_retry_migrations.py`; `linuxlab/sync/tests/test_task14_models.py`: persisted retry fields |
| 8 | `linuxlab/sync/outbox.py`, `linuxlab/sync/management/commands/sync_events.py`; `scripts/release_gate/scenarios.py`: correct existing auth-failure scenario expectation | New `linuxlab/sync/tests/test_retry_policy.py`; modify `linuxlab/sync/tests/test_task14_outbox.py`, `test_task14_flush.py`, `test_task15_backoff_settings.py`, `test_task14_command.py`, `test_task14_e2e.py`, `test_retry_migrations.py`; `scripts/tests/test_release_gate_negative_scenarios.py`: retry/fencing/real auth outcome |
| 9 | `linuxlab/sync/outbox.py`, `linuxlab/sync/management/commands/sync_events.py`: explicit reset | New `linuxlab/sync/tests/test_requeue.py`; `linuxlab/sync/tests/test_task14_command.py`: reset service/CLI |
| 10 | New `linuxlab/core/local_diagnostics.py`, `linuxlab/publication/diagnostics.py`, `linuxlab/sync/diagnostics.py`; `linuxlab/core/diagnostics.py`; `linuxlab/core/apps.py`, `linuxlab/publication/apps.py`, `linuxlab/sync/apps.py`: provider composition; new `linuxlab/core/management/__init__.py`, `linuxlab/core/management/commands/__init__.py`, `linuxlab/core/management/commands/diagnose_deployment.py`: adapter | New `linuxlab/core/tests/test_local_diagnostics.py`, `linuxlab/core/tests/test_diagnose_deployment.py`, `linuxlab/publication/tests/test_diagnostics.py`, `linuxlab/sync/tests/test_diagnostics.py` |
| 11 | `linuxlab/sync/api.py`, `linuxlab/sync/urls.py`; new `linuxlab/sync/probe.py`; `linuxlab/sync/diagnostics.py`, `linuxlab/core/management/commands/diagnose_deployment.py`: remote endpoint/client/mode | New `linuxlab/sync/tests/test_probe.py`; `linuxlab/sync/tests/test_diagnostics.py`, `linuxlab/core/tests/test_diagnose_deployment.py` |
| 12 | `linuxlab/core/management/commands/diagnose_deployment.py`, `linuxlab/sync/diagnostics.py`; new `scripts/release_gate/diagnostics.py`; `scripts/release_gate/cli.py`, `scripts/release_gate/e2e_support.py`, `scripts/release_gate/actions.py`, `scripts/release_gate/scenarios.py`: canonical release consumption and existing registration evidence adaptation | New `scripts/tests/test_release_diagnostics.py`, `scripts/tests/test_release_diagnostics_integration.py`; modify `linuxlab/core/tests/test_diagnose_deployment.py`, `linuxlab/sync/tests/test_diagnostics.py`, `scripts/tests/test_release_gate_actions.py`, `scripts/tests/test_release_gate_gate_runner.py`, `scripts/tests/test_phase1_release_gate_cli.py` |
| 13 | None | Existing and newly added suites, verification only |

Read-only anchors: `linuxlab/settings/central.py`, `node.py`, `test.py` keep
existing profile capabilities; `linuxlab/publication/signing.py` and
`linuxlab/sync/trust.py` retain fixed loaders; `scripts/phase1_release_gate.py`
retains its CLI and exit mapping; `scripts/release_gate/gate_runner.py` retains
its existing stage/cleanup orchestration; `scripts/release_gate/processes.py` retains
`wait_ready` via `/readyz`; `scripts/release_gate/evidence.py` retains schema,
assertion keys and exit algebra. `scripts/tests/test_operator_docs.py` is only
run in full verification, not edited by these tasks.

## Exact Public Interface Ledger

Definitions here are normative signatures, not production files created by
this planning task. Each interface is implemented by its owner task before a
later task consumes it. Standard-library imports are abbreviated only by their
normal type names (`Mapping`, `Callable`, `Iterable`, `Sequence`, `Literal`,
`Protocol`, `UUID`, `datetime`, `Path`). All timestamps are aware UTC.

### Core contracts — Task 1

```python
Status = Literal["PASS", "WARN", "FAIL"]
Profile = Literal["central", "node"]
Mode = Literal["local", "remote", "release"]
SafeScalar = str | int | bool | None

@dataclass(frozen=True)
class DiagnosticCheck:
    code: str
    status: Status
    message: str
    details: Mapping[str, SafeScalar] = field(default_factory=dict)

@dataclass(frozen=True)
class DiagnosticContext:
    profile: Profile
    mode: Mode
    capabilities: frozenset[str]

@dataclass(frozen=True)
class DiagnosticReport:
    schema_version: int
    profile: Profile
    mode: Mode
    overall_status: Status
    checks: tuple[DiagnosticCheck, ...]

class DiagnosticContractError(ValueError):
    pass

class DiagnosticProvider(Protocol):
    codes: frozenset[str]
    def required_codes(self, context: DiagnosticContext) -> frozenset[str]: ...
    def checks(self, context: DiagnosticContext) -> tuple[DiagnosticCheck, ...]: ...

ProviderFactory = Callable[[], DiagnosticProvider]
def register_diagnostic_provider(name: str, factory: ProviderFactory) -> None: ...
def diagnostic_providers() -> tuple[DiagnosticProvider, ...]: ...
def required_check_codes(context: DiagnosticContext, providers: Sequence[DiagnosticProvider]) -> frozenset[str]: ...
def aggregate_diagnostics(context: DiagnosticContext, checks: Iterable[DiagnosticCheck]) -> DiagnosticReport: ...
def validate_diagnostic_report(value: object, *, context: DiagnosticContext, required_codes: frozenset[str]) -> DiagnosticReport: ...
def report_to_dict(report: DiagnosticReport) -> dict[str, object]: ...
def report_to_json(report: DiagnosticReport) -> str: ...
```

Protocol bodies use Python's normal interface declaration syntax. Core imports
no module-specific implementation. `capabilities` holds the exact enabled
`LINUXLAB_CAN_*` setting names, not a second permission framework. Core knows
the eight stable check codes and safe detail vocabulary, but never performs
another app's checks. `DiagnosticContext` never contains secrets or module model
instances. Provider factories instantiate fresh providers per invocation;
module-owned configuration snapshots live only in those instances.

Task 10 adds these core orchestration signatures:

```python
def build_diagnostic_context(mode: Mode, *, settings_obj=None) -> DiagnosticContext: ...
def diagnose_deployment(context: DiagnosticContext, *, providers: Sequence[DiagnosticProvider] | None = None) -> DiagnosticReport: ...
```

`build_diagnostic_context` reads profile and the five existing CAN flags only.
It rejects nonproduction diagnostic profiles safely. `diagnose_deployment`
resolves factories once, obtains required codes, collects results, aggregates,
then calls the canonical validator on report_to_dict(report) with that same
required set. Provider errors
become safe FAIL results for their required codes; no missing required code
can disappear. Mode availability is the adapter's Task 10/11/12 rollout guard.

### Local primitives — Task 2

Keep `ReadinessResult`, `evaluate_readiness() -> ReadinessResult`, `healthz(request)`
and `readyz(request)`. Expose `check_database() -> bool`,
`check_schema_state() -> bool`, `check_profile_configuration() -> bool` from
`linuxlab.core.health` for the Task 10 core provider. Preserve private helper
aliases used by baseline tests only while converting those tests in Task 2;
the final evaluator and providers call the public names. `check_schema_state`
requires valid profile/config and an empty read-only migration plan.

### Sync configuration — Task 3

```python
@dataclass(frozen=True)
class DeferredSyncSettingError:
    setting_name: str
    code: str
    # No raw value is stored or shown; __bool__ raises the typed error.

class SyncConfigurationError(ValueError):
    # constructor stores only validated setting_name/code; fixed safe message
    def __init__(self, setting_name: str, code: str): ...

@dataclass(frozen=True)
class SyncRetryPolicy:
    base_delay: float
    max_delay: float
    max_attempts: int
    max_retry_age: float

@dataclass(frozen=True)
class NodeCredentials:
    node_id: UUID
    secret: str = field(repr=False)
    central_url: str = field(repr=False)
    allow_insecure_http: bool = False

@dataclass(frozen=True)
class SyncConfiguration:
    enabled: bool
    require_central_probe: bool
    retry_policy: SyncRetryPolicy | None
    credentials: NodeCredentials | None = field(repr=False)
    batch_max_events: int | None = None
    batch_max_bytes: int | None = None
    lease_seconds: int | None = None

def parse_sync_setting(name: str, raw: object) -> bool | int | float | DeferredSyncSettingError: ...
def load_node_identity(*, settings_obj=None) -> UUID: ...
def load_node_credentials(*, settings_obj=None) -> NodeCredentials: ...
def load_sync_configuration(*, settings_obj=None, purpose: Literal["retry", "worker", "probe", "release"] = "retry") -> SyncConfiguration: ...
def load_inbound_limits(*, settings_obj=None) -> tuple[int, int]: ...
```

These classes/functions live in `linuxlab.sync.configuration`. Importing the
module must not access Django settings or models. Settings import calls the
pure parser for known numeric/boolean names, supplying explicit default strings
only for absent environment keys. Loader calls resolve Django settings lazily
when `settings_obj` is None; tests may pass `SimpleNamespace`.

`load_node_identity` validates node UUID and nonblank secret but returns only
the UUID; credential loading reuses it and adds URL validation. This lets local
NODE_REGISTRATION diagnose identity evidence independently of URL/retry policy.
`retry` validates just the retry policy (no credentials or release switch);
its irrelevant enabled/require_central_probe fields are false and optional
configuration fields are None, not reads of unrelated deployment settings.
`worker` requires worker capability, enabled=true, retry policy, credentials,
batch sizes and lease. `probe` requires worker capability but not enabled=true,
validates credentials and retry policy per spec's remote selection matrix.
`release` reads Node release policy only on a worker-capable deployment and
requires worker prerequisites when enabled, probe prerequisites when required;
otherwise retry_policy/credentials remain None. Central never applies Node
release policy. `load_inbound_limits` validates only the two batch limits for
Central ingestion. Each loader rejects deferred errors at the capability
boundary with the same typed error; no default on malformed input.

### Registration — Task 5

```python
class NodeRegistrationError(ValueError):
    pass
class NodeRegistrationInvariantError(NodeRegistrationError):
    pass

def registration_advisory_key(node_id: UUID) -> int: ...
def lock_registration_scope(node_id: UUID) -> None: ...
def get_active_node_registration(node_id: UUID) -> NodeRegistration: ...
def registration_is_usable(registration: NodeRegistration) -> bool: ...
def issue_node_registration(display_name: str, *, node_id: UUID | None = None) -> IssuedNodeCredential: ...
def rotate_node_credential(node_id: UUID) -> IssuedNodeCredential: ...
def revoke_node_registration(node_id: UUID) -> None: ...
def authenticate_node(node_id: UUID, bearer_secret: str, now: datetime | None = None) -> NodeRegistration: ...
```

Owner: `linuxlab.sync.authentication`. Keep `IssuedNodeCredential(node_id: UUID,
secret: str)` and `NodeAuthenticationError`. `now` remains accepted for existing
auth callers but grants no previous-secret grace. `get_active_node_registration`
raises NodeRegistrationError on zero and NodeRegistrationInvariantError on
multiple ACTIVE; no choice by order. `registration_is_usable` validates UUID and
supported usable Django password hash without modifying/upgrading it.

### Transport and retry — Tasks 6 and 8

```python
class SyncOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    PERMANENT_REJECTION = "PERMANENT_REJECTION"
    AUTH_OR_REGISTRATION_FAILURE = "AUTH_OR_REGISTRATION_FAILURE"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    RATE_LIMITED = "RATE_LIMITED"

@dataclass(frozen=True)
class DeliveryFailure:
    category: SyncOutcome
    http_status: int | None = None
    error_code: str = ""
    retry_after_seconds: int | float | None = None

# In sync.transports; no policy/max_delay argument here:
def parse_retry_after(value: str | None, *, now: datetime) -> int | float | None: ...
def classify_http_failure(status: int, *, retry_after: str | None = None, now: datetime | None = None) -> TransportError: ...

# In sync.outbox, Task 8:
def compute_retry_delay(attempts: int, *, base: float, cap: float, random_source: Callable[[], float]) -> float: ...
def retry_budget_remaining(attempts: int, first_failure_at: datetime | None, *, policy: SyncRetryPolicy, now: datetime) -> bool: ...
def record_delivery_failure(claimed_row: OutboxMessage, failure: DeliveryFailure, *, policy: SyncRetryPolicy, now: datetime | None = None, random_source: Callable[[], float] | None = None, keep_claim: bool = False) -> OutboxMessage | None: ...
def claim_outbox_batch(*, max_events: int, max_bytes: int, lease_seconds: int, now: datetime | None = None) -> tuple[OutboxMessage, ...]: ...
def flush_outbox(transport: SyncTransport, *, max_events: int, max_bytes: int, lease_seconds: int, now: datetime | None = None) -> FlushSummary: ...
# Task 9:
def requeue_outbox_message(event_id: UUID) -> OutboxMessage: ...
```

`SyncOutcome` and `DeliveryFailure` live in `sync.contracts`; export the five
upper-case constants as aliases of their enum members. TransportError retains
safe string construction for existing fakes, adds `.failure: DeliveryFailure`,
and never stores raw exceptions/response bodies. AuthTransportError maps auth,
ProtocolTransportError maps permanent, TransientTransportError maps transient,
RateLimitedTransportError subclasses TransientTransportError and maps rate-limit.
RequestTooLargeError retains its subclass relationship and 413 split meaning;
its metadata category is TRANSIENT_FAILURE with safe BATCH_TOO_LARGE code.
Task 8 turns the singleton case into PERMANENT_REJECTION once, without counting
the same send twice.

`keep_claim=True` is private orchestration usage for a failed multi-event 413
send only: count the failure and apply budget while retaining a still-valid
claim for bounded smaller resends. Other failure paths release or terminalize.
Returned row is freshly loaded after the fenced write; None means stale claim.
No caller may count the same failed send twice or reuse stale in-memory attempts.
Requeue rejects invalid state with `OutboxRequeueError(ValueError)` from outbox.

### Providers/probe/release — Tasks 10–12

`CoreDiagnosticProvider`, `PublicationDiagnosticProvider`, `SyncDiagnosticProvider`
implement Task 1's protocol in their mapped modules, with zero-argument
constructors. Sync provider resolves its owned configuration once per invocation,
retaining typed errors as safe check failures, never in a global cache.

```python
# sync.probe, Task 11:
@dataclass(frozen=True)
class CentralProbeResult:
    reachable: bool
    accepted: bool
    http_status: int | None = None
    error_code: str = ""

def probe_central(credentials: NodeCredentials, *, opener=None) -> CentralProbeResult: ...
# sync.api:
class SyncProbeView(View):
    def get(self, request): ...

# scripts.release_gate.diagnostics, Task 12:
class ReleaseDiagnosticsError(RuntimeError):
    pass

def validate_release_diagnostics(stdout: str, returncode: int, *, context: DiagnosticContext, providers: Sequence[DiagnosticProvider]) -> DiagnosticReport: ...
def run_release_diagnostics(profile_env: Mapping[str, str], *, expected_profile: Profile, repo_root: Path, fixture_key_paths: tuple[Path | None, Path | None] | None = None) -> DiagnosticReport: ...
```

Release validation derives requirements from canonical providers in the target
Django process. `fixture_key_paths` is harness-only: existing isolated gate
key fixtures, never a management-command/configuration override. The tuple is
(Central signing path, Node trust path), only the matching profile's element
may be populated. Task 12 defines the isolated bridge and transport of validated
output; the gate performs no key parsing or domain inspection.

## Verification Command Conventions

Use the existing `.venv` and repository PostgreSQL configuration. If local
credentials are needed, source `.env` without printing it (`set -a`,
`source .env`, `set +a` as separate shell lines). Never commit or echo secrets.
Commands below use `env -u DJANGO_SETTINGS_MODULE` for pytest so pytest.ini
selects `linuxlab.settings.test` and scripts tests can choose their own process
profiles. Do not use SQLite, skip load-bearing PostgreSQL tests or migrate an
operator's production DB as a test fixture.

For every configuration/model/URL/app-registration task, run these exact
profile checks; migration tasks also run both drift checks. Drift checks are
also run in Task 13:

```sh
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py check
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py check
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py makemigrations --check --dry-run
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py makemigrations --check --dry-run
```

“Profile checks” and “drift checks” below refer to these exact commands, not a
choice of commands. Every task runs `.venv/bin/ruff check .` after its touched
file check because AGENTS.md requires the repository-wide check for Python
changes. A failure blocks that task's completion. Do not claim future tests
have passed from this plan's examples.

### Task 1: Core Diagnostic Contract

**Files:**
- Create: `linuxlab/core/diagnostics.py`.
- Modify: none.
- Test: create `linuxlab/core/tests/test_diagnostics.py`.

**Interfaces:**
- Consumes: standard-library immutable value/protocol patterns only.
- Produces: all Task 1 signatures in the interface ledger, schema version 1.

- [ ] Write `test_warn_aggregation` before the module exists:

```python
def test_warn_aggregation():
    from linuxlab.core.diagnostics import DiagnosticContext, DiagnosticCheck, aggregate_diagnostics
    context = DiagnosticContext("node", "local", frozenset())
    report = aggregate_diagnostics(context, [
        DiagnosticCheck("SCHEMA_STATE", "WARN", "Review schema."),
        DiagnosticCheck("DB_CONNECTIVITY", "PASS", "Database available."),
    ])
    assert report.overall_status == "WARN"
    assert tuple(c.code for c in report.checks) == ("DB_CONNECTIVITY", "SCHEMA_STATE")
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests/test_diagnostics.py::test_warn_aggregation -q`; record RED: missing diagnostics module/API.
- [ ] Implement frozen contracts and detached immutable details; tuple/frozenset inputs must not leave mutable caller aliases. Implement aggregate/serialization using one severity function:

```python
rank = {"PASS": 0, "WARN": 1, "FAIL": 2}
ordered = tuple(sorted(checks, key=lambda check: check.code))
overall = max((check.status for check in ordered), key=rank.__getitem__, default="PASS")
```

  Reject invalid input before ranking. Validator accepts either JSON text or a parsed dict, checks
  exact top-level keys, integer-not-bool schema 1, expected profile/mode,
  unique known codes, statuses and messages, required subset and recomputed
  consistency internally; consumers never repeat aggregation. JSON decoding
  rejects duplicate JSON member names as well as duplicate check codes.
- [ ] Implement limits: message ≤512 characters, details compact UTF-8 JSON
  ≤4096 bytes, whole report ≤65536 bytes. Check-specific detail allow-list:
  all codes permit `error_code` (safe upper-case machine string ≤100 chars);
  NODE_REGISTRATION permits `authority_verified` bool, `node_count` and
  `invalid_count` nonnegative ints; remote codes permit `http_status` integer
  100–599 or null. Other keys/nested structures/nonfinite values fail. No
  unknown code/secret dump can be serialized. Registry names are unique;
  identical factory re-registration is idempotent, conflicting names/codes fail.
- [ ] Rerun the focused test GREEN; extend tests for PASS, FAIL precedence,
  boolean schema, malformed shape, duplicate codes/members, invalid status/code,
  wrong profile/mode, required missing check, inconsistent overall, limits at
  and beyond boundaries, detached details, and message independence. Include
  token/password/Authorization/raw-body sentinels in rejected detail keys;
  exceptions must not echo rejected values.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests/test_diagnostics.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/core/diagnostics.py linuxlab/core/tests/test_diagnostics.py`, then `.venv/bin/ruff check .`. No Django/schema change: profile/drift checks are not required in this task.
- [ ] Run `git diff --check`; self-review spec §4 ownership, safe contract and
  no command/provider implementation. Stage only the two Files paths; commit
  `feat: add canonical deployment diagnostics contract`. Record range, verify
  clean tree, stop for independent review/adjudication.

### Task 2: Hybrid Health and Local Readiness

**Files:**
- Create: none.
- Modify: `linuxlab/core/health.py`, `linuxlab/urls.py`.
- Test: `linuxlab/core/tests/test_health.py`.

**Interfaces:**
- Consumes: existing MigrationExecutor, database connection and ReadinessResult.
- Produces: Task 2 public local primitives and four HTTP routes from the ledger.

- [ ] Write this focused test with all other local prerequisites using the
  existing healthy DB fixture:

```python
def test_ready_aliases_ignore_sync_configuration(client, settings):
    settings.LINUXLAB_PROFILE = "node"
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "invalid"
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = "invalid"
    for path in ("/ready/", "/readyz"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests/test_health.py::test_ready_aliases_ignore_sync_configuration -q`; record RED (missing canonical URL or sync-gated 503).
- [ ] Remove `_check_sync_configuration` from the evaluator and unused UUID/URL
  imports. Expose shared local functions with sanitized boolean failure. Add:

```python
path("health/", healthz, name="health"),
path("healthz", healthz, name="healthz"),
path("ready/", readyz, name="ready"),
path("readyz", readyz, name="readyz"),
```

  Place before Wagtail's catch-all. Both readiness routes share one evaluator;
  health does not invoke it. Schema check reads migration plan only, never
  executes it. Preserve generic 200/503 bodies and invalid-profile fail closed.
- [ ] Rerun focused test GREEN; replace old sync-required readiness assertions
  with local-ready assertions. Add exact alias equality, liveness zero DB
  queries, no transport/key/auth calls, DB failure, unapplied migrations,
  malformed profile/database name, blocked Central HTTP and exception sentinel
  safety. Block Central network APIs without blocking PostgreSQL's own socket.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests/test_health.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests linuxlab/sync/tests/test_offline.py -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/core/health.py linuxlab/urls.py linuxlab/core/tests/test_health.py`, `.venv/bin/ruff check .`, and both profile checks. No migration is produced; run both drift checks to confirm.
- [ ] Run `git diff --check`; review spec §3 and absence of capability probes.
  Stage exactly the three Files paths; commit
  `fix: separate local readiness from sync capability`. Record clean end/range
  and stop for review/adjudication.

### Task 3: Typed Sync / Release Configuration

**Files:**
- Create: `linuxlab/sync/configuration.py`.
- Modify: `linuxlab/settings/base.py`, `linuxlab/sync/outbox.py`, `linuxlab/sync/api.py`, `linuxlab/sync/management/commands/sync_events.py`.
- Test: create `linuxlab/sync/tests/test_configuration.py`; modify `linuxlab/sync/tests/test_task15_backoff_settings.py`, `linuxlab/sync/tests/test_task14_command.py`, `linuxlab/core/tests/test_settings_profiles.py`.

**Interfaces:**
- Consumes: fixed CAN flags and existing environment setting names.
- Produces: all Task 3 ledger types/loaders; no finite-retry behavior yet.

- [ ] Write `test_empty_retry_setting_defers_then_fails` using a SimpleNamespace
  supplying base, cap, max attempts and max age:

```python
def test_empty_retry_setting_defers_then_fails():
    from types import SimpleNamespace
    from linuxlab.sync.configuration import parse_sync_setting, load_sync_configuration, SyncConfigurationError
    import pytest
    source = SimpleNamespace(
        LINUXLAB_SYNC_BACKOFF_BASE_SECONDS=parse_sync_setting("LINUXLAB_SYNC_BACKOFF_BASE_SECONDS", ""),
        LINUXLAB_SYNC_BACKOFF_CAP_SECONDS=3600.0,
        LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS=10,
        LINUXLAB_SYNC_RETRY_MAX_AGE=86400.0,
    )
    with pytest.raises(SyncConfigurationError):
        load_sync_configuration(settings_obj=source, purpose="retry")
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_configuration.py::test_empty_retry_setting_defers_then_fails -q`; record missing typed API RED.
- [ ] Implement parser table and deferred error values, never retaining raw
  malformed input. DeferredSyncSettingError.__bool__ raises SyncConfigurationError
  so an existing truthiness consumer cannot silently enable sync on invalid input.
  Preserve base=5.0, cap=3600.0; add attempts=10,
  max_age=86400.0, require_probe=false. Booleans accept bool or stripped
  case-insensitive true/false only. Attempts are integer 1..2147483647,
  not bool or float; durations finite positive, base<cap. Check timedelta and
  aware timestamp addition for overflow before any claim/send. Keep existing
  batch defaults 100/1000000 and lease 300, positive bounded integers.

```python
# settings/base.py: absence alone receives the default; empty is not absence.
LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = parse_sync_setting(
    "LINUXLAB_SYNC_BACKOFF_BASE_SECONDS",
    os.environ.get("LINUXLAB_SYNC_BACKOFF_BASE_SECONDS", "5"),
)
```

  Apply the same table to all sync boolean/numeric values currently eagerly
  int/float parsed. Canonical base/cap names are the only accepted names, no
  competing aliases. `load_node_credentials` validates UUID, nonblank secret,
  URL host/scheme, no embedded userinfo/fragment and explicit insecure-dev flag.
- [ ] Make `_validated_backoff_bounds` delegate to the retry loader; keep
  `BackoffConfigurationError = SyncConfigurationError` as a compatibility
  exception alias, no second validator. Wire worker preflight before transport
  construction/claim; inbound API reads `load_inbound_limits` before comparing
  limits. A typed failure produces safe command failure or fixed API 503, no
  raw settings/traceback. Do not alter backoff exponent/state behavior yet.
- [ ] Rerun focused test GREEN. Add defaults, whitespace/empty, nan/inf,
  boolean traps, max-attempt bounds, base≥cap, timestamp overflow, purpose
  isolation, valid disabled Node and Central ignoring Node release flag.
  Fresh subprocess tests load both real profiles with malformed sync env,
  confirm Django setup and healthy local health/readiness survive, then prove
  required loader/worker fails with zero claim/count/network mutation. Include
  stdout/stderr/repr/log/error-value sentinel checks.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_configuration.py linuxlab/sync/tests/test_task15_backoff_settings.py linuxlab/sync/tests/test_task14_command.py linuxlab/core/tests/test_settings_profiles.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests linuxlab/sync/tests/test_task14_api.py linuxlab/sync/tests/test_task14_outbox.py -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/sync/configuration.py linuxlab/settings/base.py linuxlab/sync/outbox.py linuxlab/sync/api.py linuxlab/sync/management/commands/sync_events.py linuxlab/sync/tests/test_configuration.py`, `.venv/bin/ruff check .`, both profile checks and both drift checks.
- [ ] Run `git diff --check`; review spec §6 single parser and capability-scoped
  failure. Stage only Files paths; commit `feat: add typed sync retry configuration`.
  Record clean end/range and stop for review/adjudication.

### Task 4: NodeRegistration Lifecycle Schema + Deterministic Migration

**Files:**
- Create: `linuxlab/sync/migrations/0005_registration_lifecycle_schema.py`, `linuxlab/sync/migrations/0006_registration_lifecycle_data.py`, `linuxlab/sync/migrations/0007_registration_lifecycle_constraints.py`, `linuxlab/sync/tests/_migration_db.py`.
- Modify: `linuxlab/sync/models.py`.
- Test: create `linuxlab/sync/tests/test_registration_migrations.py`; modify `linuxlab/sync/tests/test_task14_models.py` only for schema assertions.

**Interfaces:**
- Consumes: leaf `sync.0004_outboxmessage_claim_token`, historical NodeRegistration/InboxMessage.
- Produces: lifecycle constants/state and indexed logical identity; no service semantics or new public identity.

- [ ] Write `test_ambiguous_candidates_receive_no_active_authority`. Use fresh
  scratch DB support modeled on `linuxlab/assessment/tests/test_migration_backfill.py`,
  not backward migration of the shared pytest DB. The helper exposes
  `scratch_database() -> Iterator[str]` as a context manager yielding a Django
  alias; use psycopg.sql.Identifier for generated DB names, close/remove the
  cached connection and settings alias and DROP only the created DB in finally.
  Tests unblock Django DB access explicitly. Define BEFORE=0004, EXPANDED=0005,
  AFTER=0007. Create authentic legacy rows at BEFORE; add duplicate-node rows
  at EXPANDED, where uniqueness has deliberately been relaxed.

```python
# Within the scratch fixture, after seeding two enabled rows at EXPANDED:
executor.migrate([("sync", "0007_registration_lifecycle_constraints")])
apps = executor.loader.project_state([("sync", "0007_registration_lifecycle_constraints")]).apps
Registration = apps.get_model("sync", "NodeRegistration")
assert Registration.objects.using(alias).filter(node_id=node_id, state="active").count() == 0
assert set(Registration.objects.using(alias).filter(node_id=node_id).values_list("state", flat=True)) == {"revoked"}
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_registration_migrations.py::test_ambiguous_candidates_receive_no_active_authority -q`; RED must identify absent migration/state, not infrastructure.
- [ ] Implement exact migration chain:
  0005 depends on 0004; AddField nullable state with no default, then AlterField
  node_id to UUID(default=uuid.uuid4, editable=False, db_index=True), unique=False.
  0006 depends on 0005; RunPython historical data classification, no reverse_code
  (irreversible). 0007 depends on 0006; verify no null/invalid state or >1 ACTIVE,
  AlterField state nonnull CharField(max_length=10, choices, default="revoked"),
  then allowed-state and partial uniqueness constraints. The safe revoked default
  does not grant authority; Task 5 issue explicitly writes ACTIVE.

```python
# 0006 forwards(apps, schema_editor); only historical models and DB alias.
Registration = apps.get_model("sync", "NodeRegistration")
rows = Registration.objects.using(schema_editor.connection.alias)
rows.update(state="revoked")
candidates = rows.filter(enabled=True, revoked_at__isnull=True)
unique_nodes = candidates.values("node_id").annotate(n=Count("pk")).filter(n=1)
for group in unique_nodes.iterator():
    candidates.filter(node_id=group["node_id"]).update(state="active")
```

  Import Count from Django, not runtime code. Transactional RunPython; revoked
  evidence wins. Constraint names are `node_registration_state_allowed_values`
  and `unique_active_node_registration_per_node` with condition Q(state="active").
  Keep historical enabled/hash/grace columns and PK/FK unchanged. No credentials,
  network, UUID backfill or runtime-service imports. Reverse 0007 schema normally;
  reverse through 0006 must raise IrreversibleError without guessed authority.
- [ ] Rerun focused test GREEN; add exact-one/zero/revoked/contradictory-enabled,
  varied timestamp/PK/insertion order, unusable sole hash classification,
  Inbox FK/row identity preservation, no public field, direct DB duplicate
  ACTIVE rejection, invalid-state rejection, irreversible reverse and scratch
  cleanup-completeness tests. Model-only Task 4 leaves old auth tests unchanged;
  this commit is reviewed as schema support, not deployed without Task 5.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_registration_migrations.py linuxlab/sync/tests/test_task14_models.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_task14_authentication.py linuxlab/sync/tests/test_task14_ingestion.py -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/sync/models.py linuxlab/sync/tests/_migration_db.py linuxlab/sync/tests/test_registration_migrations.py`, `.venv/bin/ruff check .`, both profile and drift checks. Migrations are Ruff-excluded by existing config; manually review historical imports and operations.
- [ ] Run `git diff --check`; review spec §§9–11, dependency graph, constraint
  order and scratch cleanup. Stage exactly Files paths; commit
  `feat: add registration lifecycle migration`. Record clean end/range and stop
  for review/adjudication.

### Task 5: NodeRegistration Services, Auth, Rotation, Revoke, CLI

**Files:**
- Create: `linuxlab/sync/tests/test_registration_concurrency.py`.
- Modify: `linuxlab/sync/authentication.py`, `linuxlab/sync/models.py`, `linuxlab/sync/api.py`, `linuxlab/sync/management/commands/manage_sync_node.py`.
- Test: `linuxlab/sync/tests/test_task14_authentication.py`, `linuxlab/sync/tests/test_task14_models.py`, `linuxlab/sync/tests/test_task14_api.py`, `linuxlab/sync/tests/test_task14_profiles.py`, `linuxlab/sync/tests/test_task14_ingestion.py`, `linuxlab/sync/tests/test_task14_processing.py`, `linuxlab/sync/tests/test_task14_e2e.py`, and the new concurrency file.

**Interfaces:**
- Consumes: Task 4 lifecycle state/constraint, existing controlled writers and password hashers.
- Produces: all Task 5 registration signatures in the ledger, unchanged IssuedNodeCredential fields.

- [ ] Replace the grace-rotation acceptance test with this new test:

```python
def test_rotation_replaces_row_without_grace():
    credential = issue_node_registration("Lab A")
    before = NodeRegistration.objects.get(node_id=credential.node_id)
    replacement = rotate_node_credential(credential.node_id)
    before.refresh_from_db()
    after = NodeRegistration.objects.get(node_id=credential.node_id, state="active")
    assert after.pk != before.pk
    assert before.state == "superseded"
    assert replacement.node_id == credential.node_id
    with pytest.raises(NodeAuthenticationError):
        authenticate_node(credential.node_id, credential.secret)
    assert authenticate_node(replacement.node_id, replacement.secret).pk == after.pk
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_task14_authentication.py::test_rotation_replaces_row_without_grace -q`; RED is old required grace argument/in-place row behavior.
- [ ] Implement deterministic registration lock key and transaction guard:

```python
def registration_advisory_key(node_id):
    digest = hashlib.sha256(b"linuxlab.sync.registration.v1\0" + node_id.bytes).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)

def lock_registration_scope(node_id):
    if not connection.in_atomic_block:
        raise NodeRegistrationError("registration transaction required")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [registration_advisory_key(node_id)])
```

  Namespace prefix isolates derivation from publication's lock; collision only
  serializes unrelated logical nodes. UUIDs are validated before lock calls.
  All three writers check `can_receive_sync_events()` themselves and use
  transaction.atomic, lock then fresh ACTIVE resolution. No row-lock-only
  workaround for an empty scope.
- [ ] Implement issue: generate logical UUID only when absent; zero ACTIVE
  inserts a new row with state ACTIVE, fresh hash, empty previous fields.
  Existing/multiple ACTIVE rejects. Rotate resolves exactly one usable ACTIVE,
  prepares new secret, writes old state SUPERSEDED then inserts ACTIVE in the
  same transaction. Revoke changes ACTIVE to REVOKED with revoked_at, no new
  row. Revoke of zero ACTIVE errors safely. Narrow `_MUTABLE_FIELDS_BY_MODEL`
  for registration to lifecycle `state`, `rotated_at`, `revoked_at`; hashes,
  enabled and old grace fields become historical immutable values.
- [ ] Implement ACTIVE-only auth using get_active_node_registration and current
  hash only, without password upgrade writes. Structural usability uses
  is_password_usable, identify_hasher and the supported hasher's decode method;
  malformed encoding produces safe failure, never a raw hash in an exception. Catch zero/multiple/unusable
  authority as generic NodeAuthenticationError. Keep safe HTTP status on the
  auth error (`http_status`, 401 default, 403 for revoked/no-active historical
  scope); resolve this inside authentication, not a second API ORM authority.
  `_authenticate_request` consumes this error and removes its enabled=False
  lookup. Never leak raw hash, secret, headers, SQL or exception chain.
- [ ] Remove grace_period/--grace-hours. CLI UUID parsers feed logical UUIDs;
  optional `issue --node-id` permits explicit zero-ACTIVE replacement.
  `IssuedNodeCredential` remains `(node_id, secret)` with secret repr=False.
  CLI translates NodeRegistrationError/ValueError into fixed safe CommandError;
  issue/rotate alone print the one-time secret. Revoke emits safe logical ID.
- [ ] Rerun focused test GREEN; replace old enabled/grace writer assertions
  with lifecycle/immutable-hash assertions. Existing ingestion/processing/E2E
  test fixture provisioning must temporarily enable Central capability around
  the explicit issue call, then restore the test's intended profile; never
  globally enable Central authority in sync conftest. Test all command flags,
  zero/multiple ACTIVE, revoked and superseded secrets, explicit replacement,
  no public field/PK selector, safe failures/repr/log/stdout/stderr (except the
  sanctioned one-time output), insertion rollback and preserved Inbox FK.
- [ ] Add PostgreSQL concurrency tests modeled on
  `test_task14_outbox.py:_RunOnOwnConnection` and publication's `_pg_sync.py`
  wait-state pattern: close_old_connections per thread, own connection, bounded
  Events/Barrier and joins, capture/re-raise worker exceptions, finally close.
  Verify actual advisory-lock waiting via pg_stat_activity, not timing sleeps.
  Two rotations both succeed serially: initial plus two new rows, one ACTIVE,
  two SUPERSEDED, only the last committed secret authenticates. Force both
  rotation/revoke orders: rotate then revoke yields zero ACTIVE; revoke first
  makes rotate fail. Issue against ACTIVE fails in either rotation order.
  Explicit issue in zero-ACTIVE scope races safely: one success, one rejection.
  Test insertion IntegrityError rolls back superseding. Multiple-ACTIVE defense
  uses a read-result stub or unconstrained scratch historical DB, never weakens
  the production constraint. Test direct DB uniqueness separately with PostgreSQL.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_task14_authentication.py linuxlab/sync/tests/test_registration_concurrency.py linuxlab/sync/tests/test_task14_models.py linuxlab/sync/tests/test_task14_api.py linuxlab/sync/tests/test_task14_profiles.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/sync/authentication.py linuxlab/sync/models.py linuxlab/sync/api.py linuxlab/sync/management/commands/manage_sync_node.py linuxlab/sync/tests`, `.venv/bin/ruff check .`, both profile checks and both drift checks (no new migration).
- [ ] Run `git diff --check`; review spec §§9–11, service authority and DB
  serialization. Stage only Files paths; commit
  `feat: enforce active-only node registration lifecycle`. Record clean end/range
  and stop for review/adjudication.

### Task 6: Semantic Transport Outcome Taxonomy

**Files:**
- Create: none.
- Modify: `linuxlab/sync/contracts.py`, `linuxlab/sync/transports.py`.
- Test: `linuxlab/sync/tests/test_task14_contracts.py`, `linuxlab/sync/tests/test_task14_transports.py`.

**Interfaces:**
- Consumes: existing SyncTransport.send_batch, BatchAck/EventAck/ACK parser, urllib.
- Produces: SyncOutcome, DeliveryFailure, error metadata, parse_retry_after and classify_http_failure from the ledger. No scheduling policy change in this task.

- [ ] Write `test_429_carries_semantic_retry_after`:

```python
def test_429_carries_semantic_retry_after():
    from datetime import UTC, datetime
    from linuxlab.sync.contracts import SyncOutcome, RateLimitedTransportError
    from linuxlab.sync.transports import classify_http_failure
    error = classify_http_failure(429, retry_after="120", now=datetime(2026, 9, 15, tzinfo=UTC))
    assert isinstance(error, RateLimitedTransportError)
    assert error.failure.category is SyncOutcome.RATE_LIMITED
    assert error.failure.retry_after_seconds == 120
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_task14_transports.py::test_429_carries_semantic_retry_after -q`; record RED missing semantic classifier.
- [ ] Implement the enum/metadata and error hierarchy in contracts. Preserve
  existing fake transport error construction with a safe fixed default message;
  `.failure` is generated from explicit safe fields, not parsed from message.
  Reject SUCCESS as a DeliveryFailure category. Map status as follows:

```python
# classify_http_failure returns an error; the caller raises it.
# 400 -> ProtocolTransportError(PERMANENT_REJECTION)
# 401/403 -> AuthTransportError(AUTH_OR_REGISTRATION_FAILURE)
# 413 -> RequestTooLargeError (existing bounded split signal)
# 429 -> RateLimitedTransportError(RATE_LIMITED, parsed retry_after)
# 408/5xx/other unknown non-2xx -> TransientTransportError(TRANSIENT_FAILURE)
```

  Successful HTTP response still returns parsed BatchAck. Network/DNS/TLS/
  timeout/OSError become fixed transient failure, never `str(exc)`. Use a
  no-redirect urllib opener (`HTTPRedirectHandler.redirect_request` returns
  None); classify 3xx conservatively transient and never forward credentials.
  Keep TLS verification and the explicit development HTTP opt-in.
- [ ] Parse Retry-After with integer delta first, then parsedate_to_datetime
  and aware UTC subtraction. Negative/expired =>0; malformed/nonfinite =>None.
  Preserve excessive valid integer durations as Python ints until policy caps
  them (avoid float overflow); bound raw header parsing to 4096 characters and
  ignore larger malformed/unprocessable metadata. No max_delay/budget in the
  parser. Do not retain raw header/body or unsafe URL in repr/errors.
- [ ] Rerun focused test GREEN; cover each status, network/TLS/timeout, unknown
  code, redirects with a spy proving zero second-origin Authorization,
  delta/date/negative/expired/malformed/excessive Retry-After and missing ACK.
  Existing ACK ambiguity tests must still pass: rejected-without-code,
  duplicate/conflicting entries and malformed JSON do not imply delivery.
  Assert stdout/stderr/log/repr/error metadata never expose sentinels for secret,
  Authorization or raw server response; no new DB failure-body storage.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_task14_transports.py linuxlab/sync/tests/test_task14_contracts.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_task14_flush.py linuxlab/sync/tests/test_task14_e2e.py -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/sync/contracts.py linuxlab/sync/transports.py linuxlab/sync/tests/test_task14_contracts.py linuxlab/sync/tests/test_task14_transports.py`, then `.venv/bin/ruff check .`. No settings/schema changes: profile/drift checks are not required.
- [ ] Run `git diff --check`; review spec §12 and unchanged ACK authority.
  Stage only Files paths; commit `feat: classify sync transport outcomes`.
  Record clean end/range and stop for review/adjudication.

### Task 7: Persisted Outbox Retry Schema + Legacy Migration

**Files:**
- Create: `linuxlab/sync/migrations/0008_outbox_retry_state.py`, `linuxlab/sync/migrations/0009_outbox_retry_age_data.py`.
- Modify: `linuxlab/sync/models.py`.
- Test: create `linuxlab/sync/tests/test_retry_migrations.py`; modify `linuxlab/sync/tests/test_task14_models.py`; reuse Task 4's `linuxlab/sync/tests/_migration_db.py` unchanged.

**Interfaces:**
- Consumes: `sync.0007_registration_lifecycle_constraints`, existing Outbox fields.
- Produces: first_failure_at, last_failure_category, last_http_status; same state vocabulary.

- [ ] Write `test_legacy_failure_age_uses_created_at_without_reset` with scratch
  BEFORE=0007, AFTER=0009, old rows created using historical apps. Capture the
  complete immutable envelope and all existing delivery fields before migration.

```python
executor.migrate([("sync", "0009_outbox_retry_age_data")])
apps = executor.loader.project_state([("sync", "0009_outbox_retry_age_data")]).apps
row = apps.get_model("sync", "OutboxMessage").objects.using(alias).get(event_id=event_id)
assert row.first_failure_at == original_created_at
assert row.attempts == original_attempts
assert row.next_attempt_at == original_due
assert row.claim_token == original_token
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_retry_migrations.py::test_legacy_failure_age_uses_created_at_without_reset -q`; record missing migration/field RED.
- [ ] Implement 0008 depends on 0007: AddField DateTimeField(null=True,
  blank=True) first_failure_at; CharField(max_length=32, blank=True, default="")
  last_failure_category; PositiveSmallIntegerField(null=True, blank=True)
  last_http_status. Constrain category to empty or the four failure values and
  HTTP status to null or 100..599. Add these names to the model-owned Outbox
  mutable-field allow-list, never envelope fields. Existing states unchanged.
- [ ] Implement 0009 depends on 0008: historical-model RunPython writes only
  first_failure_at from created_at when attempts>0; attempts=0 stays NULL.
  Do not mark any row dead-letter in migration. Preserve terminal states,
  counts, due times, leases/tokens and event/domain links. Use F expression:

```python
Outbox = apps.get_model("sync", "OutboxMessage")
Outbox.objects.using(schema_editor.connection.alias).filter(
    attempts__gt=0, first_failure_at__isnull=True,
).update(first_failure_at=F("created_at"))
```

  The timestamp is an upper-bound age anchor, not actual first failure time;
  no migration-time timestamp/fresh budget. Keep category empty and status null
  for unknown history. RunPython reverse is explicitly irreversible; it cannot
  distinguish migrated evidence from subsequent failures. 0008's structural
  reverse is ordinary schema reversal, not a runtime data-reset command.
- [ ] Rerun focused test GREEN; cover zero and positive counts for pending,
  in-flight, delivered and dead-letter rows; exact field preservation,
  old-created/recent-due timestamps, repeat algorithm idempotence, irreversible
  reverse and scratch cleanup. Tests name the age-anchor limitation explicitly.
  Create both exhausted and nonexhausted fixtures; assert migration leaves their
  state unchanged. Task 8 adds the real eligibility assertions without send.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_retry_migrations.py linuxlab/sync/tests/test_task14_models.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_registration_migrations.py linuxlab/sync/tests/test_task14_outbox.py -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/sync/models.py linuxlab/sync/tests/test_retry_migrations.py linuxlab/sync/tests/test_task14_models.py`, `.venv/bin/ruff check .`, both profile and drift checks. Inspect migration dependency/history imports manually.
- [ ] Run `git diff --check`; review spec §13 and amended conservative migration.
  Stage exactly Files paths; commit `feat: persist finite outbox retry state`.
  Record clean end/range and stop for review/adjudication.

### Task 8: Retry Policy, Worker Eligibility, Fencing

**Files:**
- Create: `linuxlab/sync/tests/test_retry_policy.py`.
- Modify: `linuxlab/sync/outbox.py`, `linuxlab/sync/management/commands/sync_events.py`, `scripts/release_gate/scenarios.py`.
- Test: `linuxlab/sync/tests/test_task14_outbox.py`, `linuxlab/sync/tests/test_task14_flush.py`, `linuxlab/sync/tests/test_task15_backoff_settings.py`, `linuxlab/sync/tests/test_task14_command.py`, `linuxlab/sync/tests/test_task14_e2e.py`, `linuxlab/sync/tests/test_retry_migrations.py`, `scripts/tests/test_release_gate_negative_scenarios.py`, and the new policy file.

**Interfaces:**
- Consumes: Task 3 policy loader, Task 6 DeliveryFailure, Task 7 persisted fields.
- Produces: Task 8 ledger functions; existing FlushSummary and outcome wrappers with corrected semantics.

- [ ] Write `test_first_failure_upper_jitter_is_base`:

```python
def test_first_failure_upper_jitter_is_base():
    from linuxlab.sync.outbox import compute_retry_delay
    assert compute_retry_delay(1, base=5.0, cap=3600.0, random_source=lambda: 1.0) == 5.0
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_retry_policy.py::test_first_failure_upper_jitter_is_base -q`; record RED: baseline first-failure ceiling is 10.
- [ ] Implement capped exponent n=max(attempts-1,0). Avoid giant powers by
  doubling a finite ceiling only until cap or n steps (bounded by float exponent
  range), clamp to cap before multiplication would overflow; no arbitrary
  exponent-64 truncation for extremely small valid base. Inject random_source,
  production default random.random at call time; accept [0,1], including 1
  in tests. Validate policy before selection/network, not only after failure.

```python
def retry_budget_remaining(attempts, first_failure_at, *, policy, now):
    if attempts >= policy.max_attempts:
        return False
    return first_failure_at is None or (now - first_failure_at).total_seconds() < policy.max_retry_age
```

  Invalid attempts>0 with no first_failure_at outside known migrated data
  fails closed as a state invariant, not unlimited age; first unfailed cycle
  is attempts=0/first_failure_at=None.
- [ ] Implement record_delivery_failure with a short atomic transaction and
  select_for_update of exact event_id/IN_FLIGHT/claim_token; additionally compare
  captured attempts so duplicate reports from the same claimed snapshot cannot
  count twice. Re-resolve, increment once, first_failure_at=existing or now,
  set category/status/safe code/reason. If permanent/auth or either budget
  exhausted, write dead_letter and clear active claim/lease/token. Otherwise
  prefer parsed Retry-After for rate-limit, cap to policy.max_delay; else jitter.
  `next_attempt_at=min(now+delay, first_failure_at+max_retry_age)`. Return the
  freshly read row, or None for stale fencing. Safe failure reasons are fixed
  literals; remote ACK reason text is never stored verbatim.
- [ ] Implement `keep_claim=True` only for multi-row 413 recovery: increment once,
  budget-check and retain live claim only for surviving rows; caller uses returned
  fresh rows to bisect. Singleton 413 counts its one failure and immediately
  terminalizes with permanent EVENT_TOO_LARGE. Splits cannot reset counters or
  resurrect an expired/terminal row. Network calls stay outside transactions.
- [ ] Update `_send_claimed_rows`: map typed failures to record_delivery_failure,
  no HTTP-number checks in retry policy. Remove 400 bisection and twice-cap
  helper; protocol failure terminalizes attempted rows. Auth failure also
  terminalizes. Existing `release_transient` and `release_auth_failure` remain
  thin wrappers for callers, delegating counting/policy. `mark_dead_letter`
  delegates permanent failure recording for a failed delivery; avoid double
  counting at its call sites. Accepted/duplicate ACKs mark only their exact
  rows delivered and retain cycle evidence; missing/ambiguous ACK rows receive
  one transient failure. Keep original envelope bytes and idempotency rules.
- [ ] Preserve select_for_update(skip_locked=True), bounded batch size and
  event-byte accounting. Due pending and expired in-flight candidates must
  pass budget before claim/send; exhausted candidates terminalize without
  increment or network and clear stale claim metadata. Future pending,
  terminal states and unexpired leases are excluded. Recheck age before every
  send; use fresh timezone.now in production, explicit now seam in deterministic
  tests. A restart reuses persisted due/age/count; crash with no recorded result
  does not invent a failure.
- [ ] Retain FlushSummary fields for compatibility: terminal failures increment
  dead_lettered; released_auth_failure becomes zero because nothing is released;
  auth_fault/protocol_fault still signal operator attention. Worker reporting
  uses safe counts, no raw reason. Update the existing revoked-credential E2E
  scenario in `scenarios.py`: assert dead_letter instead of pending while keeping
  `revoked_credential_not_accepted` evidence key and meaning. This is executable
  runtime consistency, no evidence-schema or prose reconciliation.
- [ ] Rerun focused test GREEN. Add deterministic policy tests for attempt/age
  below and equal bounds, either budget first, first/subsequent counts, stable
  first time, rate-limit delta/date/expired/malformed/excessive delay, deadline
  crossing, zero/upper jitter, large exponent, preflight failure zero writes,
  restart due preservation and migrated old/fresh candidates. Extend actual
  migration test to invoke claim on migrated rows with no transport called.
  Because claim_outbox_batch uses the default connection, run that assertion
  in a fresh Django subprocess with POSTGRES_DB set to the created scratch DB
  name and the Node profile, not by merely registering a second alias. Pass
  explicit retry limits in its environment, call claim_outbox_batch, and report
  only claimed event UUIDs and states: exhausted rows are dead_letter/unclaimed,
  nonexhausted due rows are claimed. Close the child before scratch teardown.
  Add real separate-connection stale-result/reclaim tests, insertion-free
  terminalization, multi-413 accounting and mixed ACK outcomes. Replace baseline
  unlimited/auth-released/singleton-400 expectations instead of duplicating them.
  Secret sentinels must be absent from DB failure fields, repr, stdout/stderr,
  JSON/log output and raw-body/header-derived errors.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_retry_policy.py linuxlab/sync/tests/test_task14_outbox.py linuxlab/sync/tests/test_task14_flush.py linuxlab/sync/tests/test_task15_backoff_settings.py linuxlab/sync/tests/test_retry_migrations.py linuxlab/sync/tests/test_task14_command.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests scripts/tests/test_release_gate_negative_scenarios.py -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/sync/outbox.py linuxlab/sync/management/commands/sync_events.py linuxlab/sync/tests scripts/release_gate/scenarios.py scripts/tests/test_release_gate_negative_scenarios.py`, `.venv/bin/ruff check .`, both profile and drift checks.
- [ ] Run `git diff --check`; review spec §§12–15, stale fencing and no ACK
  weakening. Stage only Files paths; commit `feat: enforce bounded sync retry policy`.
  Record clean end/range and stop for review/adjudication.

### Task 9: Explicit Outbox Requeue

**Files:**
- Create: `linuxlab/sync/tests/test_requeue.py`.
- Modify: `linuxlab/sync/outbox.py`, `linuxlab/sync/management/commands/sync_events.py`.
- Test: new requeue file and `linuxlab/sync/tests/test_task14_command.py`.

**Interfaces:**
- Consumes: Task 3 worker configuration, Task 8 controlled state writers.
- Produces: requeue_outbox_message(event_id: UUID) -> OutboxMessage and OutboxRequeueError.

- [ ] Write `test_requeue_resets_only_outbox_cycle`; use `_make_outbox`'s exact
  field factory pattern from test_task14_outbox.py to create a dead_letter row
  with attempts=3, first_failure_at, failure category and an existing DomainEvent
  trace. Explicitly set Node worker capability, sync enabled and valid local
  credentials/settings in a local fixture. Capture all DB rows before the call.

```python
before_events = list(DomainEvent.objects.values())
before_ids = set(OutboxMessage.objects.values_list("event_id", flat=True))
requeued = requeue_outbox_message(row.event_id)
assert requeued.state == "pending"
assert requeued.attempts == 0
assert requeued.first_failure_at is None
assert requeued.domain_event_id == row.domain_event_id
assert list(DomainEvent.objects.values()) == before_events
assert set(OutboxMessage.objects.values_list("event_id", flat=True)) == before_ids
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_requeue.py::test_requeue_resets_only_outbox_cycle -q`; record missing requeue API RED.
- [ ] Implement service: validate UUID and required local worker configuration,
  check Node capability, transaction.atomic and select_for_update exact event_id;
  reject unknown or non-dead_letter. Through model-owned writer set pending,
  attempts=0, first_failure_at=None, next_attempt_at=timezone.now(), clear
  claimed_at/lease_until/claim_token, last_failure_category="", last_http_status=None,
  rejection_code/rejection_reason="". Preserve envelope, event UUID, domain trace,
  created_at and delivered_at history. Return refreshed row. No publish_event,
  transport call, remote repair proof or additional row insertion.
- [ ] Extend sync_events argument parser with optional positional action
  restricted to `requeue`, optional --event-id, and existing --once. With no
  action preserve worker forms; with requeue require --event-id and reject
  --once; --event-id without requeue errors. Requeue dispatches before worker
  loop/signal setup and prints only `requeued <event_uuid>` after success.
  Fixed safe CommandError for invalid state/config; no payload or raw errors.
- [ ] Rerun focused test GREEN; add all wrong states, invalid/unknown UUID,
  capability denial, malformed config zero writes, exact reset timestamp,
  failure rollback, two concurrent reset requests (one transition, other wrong
  state), zero DomainEvent/outbound creation, no recursion, immutable envelope
  equality, no network and secret/payload absence in stdout/stderr/repr/logs.
  Diagnostics are not connected to requeue.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_requeue.py linuxlab/sync/tests/test_task14_command.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_retry_policy.py linuxlab/sync/tests/test_task14_outbox.py linuxlab/sync/tests/test_task14_flush.py -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/sync/outbox.py linuxlab/sync/management/commands/sync_events.py linuxlab/sync/tests/test_requeue.py linuxlab/sync/tests/test_task14_command.py`, `.venv/bin/ruff check .`, both profile checks; no schema change, both drift checks must remain clear.
- [ ] Run `git diff --check`; review amended spec §15 and
  NO_EXISTING_PATTERN_EVENT_REMOVED. Stage only Files paths; commit
  `feat: add explicit outbox requeue`. Record clean end/range and stop for
  review/adjudication.

### Task 10: Module Diagnostic Providers + LOCAL Command

**Files:**
- Create: `linuxlab/core/local_diagnostics.py`, `linuxlab/publication/diagnostics.py`, `linuxlab/sync/diagnostics.py`, `linuxlab/core/management/__init__.py`, `linuxlab/core/management/commands/__init__.py`, `linuxlab/core/management/commands/diagnose_deployment.py`.
- Modify: `linuxlab/core/diagnostics.py`, `linuxlab/core/apps.py`, `linuxlab/publication/apps.py`, `linuxlab/sync/apps.py`.
- Test: create `linuxlab/core/tests/test_local_diagnostics.py`, `linuxlab/core/tests/test_diagnose_deployment.py`, `linuxlab/publication/tests/test_diagnostics.py`, `linuxlab/sync/tests/test_diagnostics.py`.

**Interfaces:**
- Consumes: Task 1 contracts/registry, Task 2 local primitives, Task 3 configuration, Task 5 registration services, unchanged A signing/trust loaders.
- Produces: three provider classes, build_diagnostic_context and diagnose_deployment from the ledger, LOCAL command adapter.

- [ ] Write `test_local_node_credentials_are_unverified_warn`. In a local fixture
  set the real Node capability flags/profile, valid config and monkeypatch the
  fixed public-key path to a tmp_path Ed25519 test key. Do not change production
  paths or add CLI overrides. Patch Central opener to raise AssertionError if
  called and capture full relevant row values before diagnosis:

```python
out = io.StringIO()
call_command("diagnose_deployment", "--mode", "local", "--format", "json", stdout=out)
report = json.loads(out.getvalue())
checks = {check["code"]: check for check in report["checks"]}
assert checks["NODE_REGISTRATION"]["status"] == "WARN"
assert checks["NODE_REGISTRATION"]["details"]["authority_verified"] is False
assert "CENTRAL_CONNECTIVITY" not in checks
assert report["overall_status"] == "WARN"
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests/test_diagnose_deployment.py::test_local_node_credentials_are_unverified_warn -q`; record RED missing command.
- [ ] Implement module registration via each AppConfig.ready; retain sync's
  existing enqueue registration. Factories are classes, never initialized at
  startup. The two empty management package initializers are import scaffolding,
  not a new subsystem. Core provider calls check_database/check_schema_state;
  both required, DB failure also fails SCHEMA_STATE. Other providers call only
  their own loaders/services. Do not import module providers in core.

```python
# In PublicationConfig.ready; corresponding owners register their own classes.
from linuxlab.core.diagnostics import register_diagnostic_provider
from .diagnostics import PublicationDiagnosticProvider
register_diagnostic_provider("publication", PublicationDiagnosticProvider)
```

- [ ] Implement exact LOCAL selection: core two checks always; publication
  SIGNING_KEY only CAN_AUTHOR; sync TRUST_KEY only CAN_IMPORT_PACKAGES;
  Central NODE_REGISTRATION only CAN_RECEIVE_SYNC_EVENTS; Node NODE_REGISTRATION
  and SYNC_RETRY_POLICY when outbound sync enabled. A malformed Node
  require-probe value still emits SYNC_RETRY_POLICY FAIL, never false fallback.
  Central doesn't apply Node policy. Sync provider reads relevant settings into
  a private snapshot on construction; required_codes and checks use the same
  snapshot and typed loader results. A deferred enabled value is a required
  capability-config FAIL, not truthiness-based enablement.
- [ ] Central registration check groups existing node_id inventory and verifies
  one usable ACTIVE for each; zero rows inventory PASS/node_count=0; historical
  rows with ACTIVE generate no warning; history-only logical node FAIL. Use
  aggregate counts plus bounded row iteration for hash usability, no huge
  output lists. Node credential check uses load_node_identity: malformed UUID
  or blank secret FAIL; configured credentials WARN/authority_verified=false,
  never claims Central authority. SYNC_RETRY_POLICY owns URL/policy failures;
  no remote call is made to distinguish them. Catch only safe typed/config/DB
  errors as owned FAILs; orchestrator catches unexpected provider failure with
  fixed PROVIDER_FAILED metadata and never prints raw exceptions.
- [ ] Implement adapter defaults local/text, choices for all three approved
  modes and text/json. Until Tasks 11/12, reject remote/release with a fixed
  nonzero error before running checks; never silently run local under another
  mode. No profile/skip/force/remediation options. `requires_system_checks=[]`
  and no migration banner; JSON stdout is exactly one report. On valid FAIL
  write the JSON then raise CommandError with returncode=1 and fixed safe stderr;
  PASS/WARN return normally. Text renders that same report, not another evaluator.
- [ ] Rerun focused test GREEN; cover central/node/disabled matrices, no startup
  DB/network, all key-loader typed failures, valid wrong signing key remaining
  only structurally valid (no speculative remote trust match), profile spoof,
  unavailable modes, unknown flags, provider errors, report limits and text/JSON
  semantic equality. Compare all persistent fields and fixture file bytes before/
  after successful and failed diagnostics. Spy on issue/rotate/revoke/requeue/
  flush/publish_event and assert none called. Test secret/password/private-key/
  Authorization/raw-body sentinels absent from stdout/stderr/repr/JSON/logs and
  DB error fields; no generic sanitizer implementation.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests/test_local_diagnostics.py linuxlab/core/tests/test_diagnose_deployment.py linuxlab/publication/tests/test_diagnostics.py linuxlab/sync/tests/test_diagnostics.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests linuxlab/publication/tests/test_signing.py linuxlab/sync/tests/test_trust.py linuxlab/sync/tests/test_task14_authentication.py -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/core linuxlab/publication/diagnostics.py linuxlab/publication/apps.py linuxlab/publication/tests/test_diagnostics.py linuxlab/sync/diagnostics.py linuxlab/sync/apps.py linuxlab/sync/tests/test_diagnostics.py`, `.venv/bin/ruff check .`, both profile and drift checks.
- [ ] Run `git diff --check`; review spec §§4–6, fixed trust, zero mutation and
  Node WARN evidence distinction. Stage only Files paths; commit
  `feat: add local deployment diagnostics`. Record clean end/range and stop for
  review/adjudication.

### Task 11: Dedicated Remote Central Probe + REMOTE Diagnostics

**Files:**
- Create: `linuxlab/sync/probe.py`.
- Modify: `linuxlab/sync/api.py`, `linuxlab/sync/urls.py`, `linuxlab/sync/diagnostics.py`, `linuxlab/core/management/commands/diagnose_deployment.py`.
- Test: create `linuxlab/sync/tests/test_probe.py`; modify `linuxlab/sync/tests/test_diagnostics.py`, `linuxlab/core/tests/test_diagnose_deployment.py`.

**Interfaces:**
- Consumes: authenticate_node and safe auth adapter, Task 3 NodeCredentials, Task 6 no-redirect HTTP mechanics, Task 10 provider orchestration.
- Produces: CentralProbeResult, probe_central and SyncProbeView.get from the ledger; explicit REMOTE command mode.

- [ ] Write `test_probe_accepts_active_credentials_without_mutation`. Configure
  Central receive capability, issue a test credential before taking snapshots:

```python
before = {
    model: list(model.objects.order_by("pk").values())
    for model in (NodeRegistration, InboxMessage, OutboxMessage, DomainEvent, Session)
}
response = client.get("/api/v1/sync/probe/",
    HTTP_X_LINUXLAB_NODE_ID=str(credential.node_id),
    HTTP_AUTHORIZATION=f"Bearer {credential.secret}")
assert response.status_code == 200
assert response.json() == {"status": "ok"}
for model, values in before.items():
    assert list(model.objects.order_by("pk").values()) == values
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_probe.py::test_probe_accepts_active_credentials_without_mutation -q`; record RED missing endpoint.
- [ ] Add `path("api/v1/sync/probe/", SyncProbeView.as_view(), name="sync-probe")`
  in sync URLs beside events before catch-all. Check receive capability first;
  parse same headers and use same `_authenticate_request`/authenticate_node as
  ingestion. GET returns fixed 200 body; malformed/missing/rejected credential
  returns safe 401/403. Wrong profile 403. Other methods 405. No session login,
  password-hash upgrade, event ingestion or persistent state write.
- [ ] Implement probe_central: build configured URL plus `/api/v1/sync/probe/`,
  GET with headers only, no query/body secrets, no-redirect urllib opener,
  certificate verification, timeout=30. Read at most 4097 bytes and reject when
  >4096, parse exactly the success object for 200. No automatic retries and no
  raw headers/body in CentralProbeResult. `opener=None` builds production opener;
  tests inject an object with `.open(request, timeout=30)`.

```python
# Concrete evidence mapping, not retry taxonomy:
# accepted 200 + exact safe body -> reachable=True, accepted=True
# HTTP response including 401/403/5xx/redirect -> reachable=True, accepted=False
# malformed/oversized 200 -> reachable=True, accepted=False
# DNS/timeout/TLS/network -> reachable=False, accepted=False
```

  3xx is rejected without forwarding Authorization. Under the existing explicit
  dev/test HTTP flag only, insecure URLs remain available for isolated tests.
  Missing local config yields two FAIL checks without network. Errors use fixed
  codes PROBE_UNREACHABLE, AUTH_REJECTED, PROBE_RESPONSE_INVALID or CONFIG_INVALID.
- [ ] Enable REMOTE in command for Node only. Sync provider adds both remote
  codes to required_codes, validates probe prerequisites, calls probe_central
  exactly once and maps reachability/auth booleans to two checks. LOCAL remains
  zero-network; Central REMOTE rejected before network. Keep local registration
  WARN even when remote auth passes, since that check's evidence did not change.
  RELEASE stays explicitly unavailable until Task 12, not mapped to REMOTE.
- [ ] Rerun focused test GREEN; add revoked/old-secret rejection, zero/multiple
  authority, profile/method restrictions, missing prerequisites, 401/403,
  408/429/5xx, DNS/TLS/timeout, redirects with no forwarded secret, bad/oversize
  body and 30-second timeout assertion. Compare full DB/session rows and retry
  metadata before/after both server and client diagnostic success/failure.
  Assert zero key/credential creation, no sync retry, readiness unchanged while
  opener fails. Sentinel assertions cover stdout/stderr/repr/JSON/logs and DB
  failures, including exception bodies containing token/Authorization values.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_probe.py linuxlab/sync/tests/test_diagnostics.py linuxlab/core/tests/test_diagnose_deployment.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests/test_health.py linuxlab/sync/tests/test_task14_api.py linuxlab/sync/tests/test_task14_transports.py linuxlab/sync/tests/test_offline.py -q`.
- [ ] Run `.venv/bin/ruff check linuxlab/sync/probe.py linuxlab/sync/api.py linuxlab/sync/urls.py linuxlab/sync/diagnostics.py linuxlab/core/management/commands/diagnose_deployment.py linuxlab/sync/tests/test_probe.py`, `.venv/bin/ruff check .`, both profile and drift checks.
- [ ] Run `git diff --check`; review spec §7 fixed endpoint/read-only boundaries.
  Stage only Files paths; commit `feat: add read-only Central sync probe`.
  Record clean end/range and stop for review/adjudication.

### Task 12: RELEASE Diagnostics + Release-Gate Integration

**Files:**
- Create: `scripts/release_gate/diagnostics.py`.
- Modify: `linuxlab/core/management/commands/diagnose_deployment.py`, `linuxlab/sync/diagnostics.py`, `scripts/release_gate/cli.py`, `scripts/release_gate/e2e_support.py`, `scripts/release_gate/actions.py`, `scripts/release_gate/scenarios.py`.
- Test: create `scripts/tests/test_release_diagnostics.py`, `scripts/tests/test_release_diagnostics_integration.py`; modify `linuxlab/core/tests/test_diagnose_deployment.py`, `linuxlab/sync/tests/test_diagnostics.py`, `scripts/tests/test_release_gate_actions.py`, `scripts/tests/test_release_gate_gate_runner.py`, `scripts/tests/test_phase1_release_gate_cli.py`.

**Interfaces:**
- Consumes: core canonical contracts/orchestration, module providers, sync typed release policy, probe; existing dual_deployment lifecycle and gate StartupError.
- Produces: both release helper signatures in the ledger; RELEASE command semantics. Existing `scripts/phase1_release_gate.py`, `run_release_gate` signature, evidence schema and exit-code algebra are preserved.

- [ ] Write `test_release_validator_rejects_missing_required_code` with an
  explicit context and pure fake provider implementing Task 1 protocol:

```python
class RequiredTrustProvider:
    codes = frozenset({"TRUST_KEY"})
    def required_codes(self, context):
        return self.codes
    def checks(self, context):
        return ()

def test_release_validator_rejects_missing_required_code():
    context = DiagnosticContext("node", "release", frozenset({"LINUXLAB_CAN_IMPORT_PACKAGES"}))
    text = '{"schema_version":1,"profile":"node","mode":"release","overall_status":"PASS","checks":[]}'
    with pytest.raises(ReleaseDiagnosticsError):
        validate_release_diagnostics(text, 0, context=context, providers=(RequiredTrustProvider(),))
```

- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest scripts/tests/test_release_diagnostics.py::test_release_validator_rejects_missing_required_code -q`; record missing release helper RED.
- [ ] Enable RELEASE in the management command. In SyncDiagnosticProvider,
  canonical purpose=release configuration decides requirements: Node policy
  false omits both remote checks, true requires both and probe prerequisites;
  malformed switch emits SYNC_RETRY_POLICY FAIL. Disabled sync/false policy
  requires only applicable local checks; Central ignores Node policy. No CLI
  switch can override policy. Share REMOTE probe implementation without making
  ordinary LOCAL diagnosis read Central.
- [ ] Implement validate_release_diagnostics: reject returncode!=0, input over
  65536 UTF-8 bytes, malformed JSON/duplicate members, wrong schema/profile/mode,
  missing/duplicate/invalid code/status, inconsistent overall or FAIL. Required
  codes come from `required_check_codes(context, providers)` and never from the
  supplied report. Pass raw stdout into `validate_diagnostic_report` so core
  performs duplicate-member-safe JSON decoding; no script aggregation,
  HTTP mapping, model query or human-message parsing. WARN passes.
- [ ] Implement the exact target-process bridge below so deployment policy
  validation is not reconstructed by the outer gate process's Central settings:

```text
run_release_diagnostics(profile_env, expected_profile, repo_root, fixture_key_paths)
  -> subprocess: python -m scripts.release_gate.diagnostics --expected-profile central|node
     environment = actual profile_env + inherited deployment env
     stdin = isolated fixture paths only when supplied, otherwise empty object
     target process django.setup()
     context = build_diagnostic_context("release")
     verify context.profile == expected_profile
     providers = diagnostic_providers()  # same target configuration snapshot
     execute canonical diagnostic command in child process with identical env
     validate_release_diagnostics(child.stdout, child.returncode, context, providers)
     emit canonical report JSON only; exit 0 for PASS/WARN, nonzero otherwise
  -> parent checks target process exit and canonical schema/profile/mode,
     returns DiagnosticReport or raises safe ReleaseDiagnosticsError
```

  Define the module entry function as
  `target_release_diagnostics(expected_profile: Profile, *, fixture_key_paths: tuple[Path | None, Path | None] | None = None) -> int`.
  In a normal deployment, its child argv is exactly
  `[sys.executable, "manage.py", "diagnose_deployment", "--mode", "release", "--format", "json"]`.
  Target validator, not a report's claimed requirement list, is the gate authority
  for capability completeness. Parent validates the returned report structure
  with Task 1 core validator; target-process tests must inject corrupt *command*
  stdout to prove missing capability codes fail before export to parent.
  Context and provider construction perform no network or data writes.
- [ ] Isolated gate fixture path uses the existing harness bridge in cli.py:
  add action `diagnose-release`; define
  `_cmd_diagnose_release(args) -> None`, reading fixture paths from existing
  sensitive stdin helper, substituting only the fixed loader path attributes
  for that short-lived test process, then calling the real unmodified command
  with `call_command("diagnose_deployment", "--mode", "release", "--format", "json")`.
  This action's stdout/exit must bypass cli.py's ordinary JSON action wrapper:
  no last-line extraction, no enclosing success/error payload, no swallowed
  CommandError returncode. It parses no key material. This is the same isolated
  loader-boundary substitution pattern already in
  `scripts/tests/_package_operator_subprocess.py`; production command accepts
  no alternate paths and imports no script helper.
  Target supervisor selects this fixture child only when the existing harness
  supplies its already-created key paths; no fixture setup or key generation
  runs in diagnostics. Do not expose secrets/paths in command failures.
- [ ] Bound command child timeout to 60 seconds and supervisor to 120 seconds;
  capture output through bounded reads, fail if stdout exceeds 65536 bytes,
  discard raw stderr after a fixed safe error. Do not reuse `run_action`'s
  last-line JSON parsing/error echo. Treat crash/timeout/nonzero/malformed output
  as ReleaseDiagnosticsError with fixed message/code; no raw stdout/stderr,
  env, authorization, key material or response text in exception/repr/logs.
  Use Popen with bounded pipe reads; stop/drain on output overflow. Launch the
  supervisor in its own process group and terminate/reap that group on timeout
  so an inner diagnostic child cannot leak after the supervisor exits. Include
  a timeout/oversize integration test proving both processes are gone.
  Both processes inherit the actual deployment DB; never substitute `postgres`.
- [ ] In dual_deployment, after explicit fixture provisioning/migrations/key
  setup and before yield, call run_release_diagnostics for Central and Node,
  passing existing profile environments and only each profile's own fixture
  key path. Wrap failure as StartupError without raw exception text. Even a
  scenario intentionally stopping Central later runs its initial release check
  before inducing the fault. No readiness/capability check may repair fixtures
  after a failure. Gate runner already holds Stage.STARTUP until factory yield;
  preserve this mapping and cleanup precedence without modifying its authority
  machinery. `wait_ready` remains local `/readyz`; `_run_validation_checks`
  remains separate model/drift checks. Verify the real factory invokes
  both canonical checks and failure maps to exit 3 with cleanup intact.
- [ ] Adapt existing registration evidence: actions.find_node_registration
  calls sync.authentication.get_active_node_registration, returns None for
  zero ACTIVE, propagates invariant error; no `.first()`/direct registration
  query. cli.py emits `state`, scenarios compare `state == "active"` instead
  of legacy enabled. Keep existing evidence proof/assertion names and schema.
  These reads are separate E2E evidence, never diagnostic remediation. No
  direct signing/trust/auth/HTTP inspection in the release validator.
- [ ] Rerun focused test GREEN. Matrix tests include supported PASS/WARN,
  overall FAIL, malformed JSON, unsupported/bool schema, wrong profile/mode,
  duplicate JSON members/check codes, required missing check, invalid status,
  command crash/timeout/nonzero, inconsistent aggregate and message changes.
  Integrations use two real isolated DB/profile processes and fixture keys;
  require_probe=false with Central stopped gives local-ready and local RELEASE
  success; true gives local-ready but remote/gate FAIL; rejected credential
  gives auth FAIL; disabled sync and malformed switch fail/select exactly per
  spec. Compare DB fields/file bytes across probes, no fixture auto-repair.
  Test mismatched parent/target profiles, actual deployment DB selection,
  no env-policy parsing in scripts, and no production imports of harness helpers.
  Inject secret/private-key/password/Authorization/raw-body sentinels into
  command failures and verify absence in stdout/stderr/JSON/repr/logs, existing
  release evidence and DB error fields.
- [ ] Run `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest scripts/tests/test_release_diagnostics.py scripts/tests/test_release_diagnostics_integration.py linuxlab/core/tests/test_diagnose_deployment.py linuxlab/sync/tests/test_diagnostics.py -q`, then `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest scripts/tests linuxlab/core/tests linuxlab/sync/tests/test_probe.py -q`.
- [ ] Run `.venv/bin/ruff check scripts/release_gate/diagnostics.py scripts/release_gate/cli.py scripts/release_gate/e2e_support.py scripts/release_gate/actions.py scripts/release_gate/scenarios.py scripts/tests/test_release_diagnostics.py scripts/tests/test_release_diagnostics_integration.py linuxlab/sync/diagnostics.py linuxlab/core/management/commands/diagnose_deployment.py`, `.venv/bin/ruff check .`, both profile and drift checks.
- [ ] Run `git diff --check`; review spec §§5,7,8 and unchanged release evidence
  schema/exit algebra. Stage only Files paths; commit
  `feat: gate releases on canonical diagnostics`. Record clean end/range and
  stop for review/adjudication.

### Task 13: Integrated Verification / Corrective B Final Gate

**Files:** none. **Interfaces:** consume all completed behavior; produce only an
execution report. No edits, no commits, no automatic fixes in this task. If a
check fails, report the exact failing HEAD/test and return it to adjudication;
do not hide it with skipped tests or fold a fix into verification.

- [ ] Require exact reviewed Task 12 HEAD and clean tree; record full SHA,
  branch and `git diff --check` result. Snapshot residual DB names using the
  read-only query below before tests, so pre-existing user databases are never
  mistaken for this run's residue. Credentials stay in environment.

```sh
.venv/bin/python - <<'PY'
import os
import psycopg
with psycopg.connect(dbname="postgres", user=os.environ.get("POSTGRES_USER", "linuxlab"), password=os.environ.get("POSTGRES_PASSWORD", ""), host=os.environ.get("POSTGRES_HOST", "127.0.0.1"), port=os.environ.get("POSTGRES_PORT", "5432")) as conn:
    with conn.cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database WHERE datname LIKE 'test_migration_scratch_%' OR datname LIKE 'linuxlab_gate_%' ORDER BY datname")
        print([row[0] for row in cursor.fetchall()])
PY
```

- [ ] Run focused core diagnostics/readiness/command tests:

```sh
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests/test_diagnostics.py linuxlab/core/tests/test_health.py linuxlab/core/tests/test_local_diagnostics.py linuxlab/core/tests/test_diagnose_deployment.py -q
```

- [ ] Run registration, PostgreSQL concurrency and actual migrations:

```sh
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_task14_authentication.py linuxlab/sync/tests/test_registration_concurrency.py linuxlab/sync/tests/test_registration_migrations.py linuxlab/sync/tests/test_retry_migrations.py -q
```

- [ ] Run transport, retry, fencing, restart and requeue suites:

```sh
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/sync/tests/test_configuration.py linuxlab/sync/tests/test_task14_contracts.py linuxlab/sync/tests/test_task14_transports.py linuxlab/sync/tests/test_task14_outbox.py linuxlab/sync/tests/test_task14_flush.py linuxlab/sync/tests/test_retry_policy.py linuxlab/sync/tests/test_task15_backoff_settings.py linuxlab/sync/tests/test_requeue.py linuxlab/sync/tests/test_task14_command.py -q
```

- [ ] Run providers, probes and release integration:

```sh
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/publication/tests/test_diagnostics.py linuxlab/sync/tests/test_diagnostics.py linuxlab/sync/tests/test_probe.py scripts/tests/test_release_diagnostics.py scripts/tests/test_release_diagnostics_integration.py -q
```

- [ ] Run directly related complete app/runtime suites, scripts, then the full repository suite; record exact counts and investigate every unexpected skip:

```sh
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest linuxlab/core/tests linuxlab/sync/tests linuxlab/publication/tests linuxlab/runtime/tests -q
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest scripts/tests -q
env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest -q
.venv/bin/ruff check .
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py check
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py check
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py makemigrations --check --dry-run
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py makemigrations --check --dry-run
```

- [ ] Repeat the residual DB query; require no newly leaked scratch/gate DB.
  Existing scratch fixture cleanup checks must also prove Django alias/cache
  cleanup. Do not drop unknown/pre-existing databases. If the release test
  produces ignored evidence artifacts, preserve its existing cleanup policy;
  no tracked/untracked repository changes may remain.
- [ ] Run `git rev-parse HEAD`, `git branch --show-current`, `git status --short`,
  `git diff --check`; require unchanged exact Task 12 SHA and clean branch.
  Report all commands, exact results, migration names and review concerns.
  **NO COMMIT.** Stop for independent final verification review/adjudication.

## Spec Coverage Matrix

| Binding spec section | Implementation/review responsibility |
|---|---|
| §§1–2 authority, scope, concrete names | Global Constraints, File Responsibility Map, all task review boundaries |
| §3 hybrid readiness | Task 2; fresh-process config regression Task 3; offline release Task 12 |
| §4 diagnostic contract, ownership, read-only, limits | Tasks 1 and 10; mutation/secrecy tests Tasks 10–12 |
| §5 capabilities/modes/command | Tasks 10–12; typed source Task 3 |
| §6 typed deployment policy | Task 3; capability failures Tasks 8,10,12 |
| §7 dedicated remote probe | Task 11; policy consumption Task 12 |
| §8 thin release gate | Task 12; full verification Task 13 |
| §§9–11 registration identity/lifecycle/migration | Tasks 4–5; diagnostic inventory Task 10 |
| §12 semantic transport/ACK | Task 6; consumer behavior and 413 budget Task 8 |
| §13 persisted retry/legacy migration | Task 7; actual migrated-row eligibility Task 8 |
| §14 finite budget, jitter, Retry-After | Task 8, consuming Task 6 metadata |
| §15 worker/fencing/requeue | Tasks 8–9; no new DomainEvent enforced Task 9 |
| §16 behavioral verification | Every focused/broader test checklist; integrated Task 13 |
| §17 rollout dependency order | Tasks 1–13, with explicit schema/behavior review boundary clarifications |
| §18 deferred scope | Global Constraints only; no implementation tasks for excluded work |
| §19 acceptance invariants | Global Constraints; per-task self-review and Task 13 |
| §20 self-review conclusion | This plan's consistency audit and each independent review |

## Plan Consistency and Review Audit

The 12 implementation commits isolate contract, local HTTP semantics, typed
configuration, registration schema, registration services, transport taxonomy,
retry schema, retry behavior, reset, local providers, remote probe and release
consumption. Task 13 tests the integrated exact HEAD without modifying it.
Schema tasks exercise real forward historical migrations in independently
created scratch DBs; the application DB is never rewound through an irreversible
migration. Migration chain is 0004 → 0005 → 0006 → 0007 → 0008 → 0009; only
Task 4 and Task 7 create migrations.

All public signatures consumed by a later task are defined in the ledger at
their producing task. Provider-required codes and output validation share one
context/provider snapshot; release validation takes place under the target
profile, not outer-process settings or report-provided requirements. Core
contains no sync/publication implementation import. Deployment defaults and
mode authority are those of the approved spec.

Registration concurrency retains the PostgreSQL constraint plus UUID-scoped
advisory serialization. Outbox concurrency retains short locked claims,
claim-token fencing and network outside transactions. Every failure accounting
path, including 413 splits and partial ACKs, has one failed-send increment and
finite budget; restart never resets it. Explicit reset does not create events.

Review boundaries remain testable even where schema precedes behavior:
Task 4 asserts schema/migration only, Task 5 closes runtime authority; Task 7
asserts preserved retry history, Task 8 exercises budget eligibility. Task 10
rejects unavailable modes explicitly until Tasks 11/12 enable them. No intermediate
commit is represented as the final Corrective B runtime release.

Task 8 is one cohesive state-machine review: splitting counting, fencing and
selection across independently accepted behavior commits would leave unsafe
retry paths. Task 12 is one cohesive trust-boundary review: command invocation,
canonical requirement validation and gate failure mapping are verified together.
Their focused suites precede broader regressions; neither includes unrelated
refactoring or evidence-schema redesign.

No architectural question is delegated to the implementer. New findings that
make the approved spec literally impossible must stop execution with exact
code/spec evidence rather than silently weakening it. This plan does not claim
implementation or tests have run during authoring.
