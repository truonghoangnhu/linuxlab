"""Task 15 Task 11 (+ correctives v2-v7): release-gate orchestration --
preflight, the required scenario sequence (each in its own real
dual_deployment(), exactly mirroring how the Task 8-10 pytest suite
already isolates them), evidence assembly, cleanup, and the approved
exit-code mapping.

Deliberately separated from scripts/phase1_release_gate.py (the thin
argv/CLI layer) so this orchestration is independently testable without
going through subprocess/argv parsing -- and deliberately
dependency-injectable (`scenario_specs`, `dual_deployment_factory`,
`environment_builder`) so ORCHESTRATION correctness (exit-code mapping,
failure precedence, evidence sanitization) can be proven with fast
fakes, reserving real Postgres/Gunicorn cycles for the small number of
genuine end-to-end proof tests.

Exit-code / failure-category mapping is exactly
scripts.release_gate.evidence.EXIT_CODE, per the approved Task 15
design spec Section 18 ("Failure Model"):

    0 = PASS
    2 = PRECHECK_FAILURE  (environment/repo/dependency/db-safety/config)
    3 = STARTUP_FAILURE   (db migration, Gunicorn startup, readiness)
    4 = SCENARIO_FAILURE  (happy-path/negative/recovery/invariant failure)
    5 = CLEANUP_FAILURE   (required cleanup cannot be completed)

Task 15 Task 11 corrective v7 rewrites the terminal-truth model to be
LITERAL rather than behaviorally-equivalent (the v6 review's core
finding): one `RunState` object (below) is the single authoritative
home for lifecycle stage, every failure category, resource ownership,
publication outcome, and the final decision -- nothing here duplicates
that state in a second, potentially-divergent local variable. A real
`ResourceLedger` tracks execution-resource ownership by (kind, safe_id)
pairs, acquired/released as cleanup outcomes become known, and
reconciled once more after the run's own outer-root cleanup (Section 15:
a workspace whose own cleanup failed but that outer-root removal later
swept away regardless must not be reported as still retained). PASS is
now sealed behind actual publication success (Section 18-20): even a
candidate that satisfies every other invariant cannot become PASS/0
unless `publish_evidence()` genuinely ran and succeeded. And the
last-resort terminal path (when even FINALIZE's own fail-safe machinery
breaks) selects from fully prebuilt, fixed-content emergency results
(Section 7-8) rather than trying to construct anything dynamic -- so a
persistent failure that would have defeated a dynamically-built fallback
can never escape, and an already-known scenario primary exit code
survives even that terminal collapse.

There is no distinct "internal/unexpected error" category. An
unexpected exception is classified by the explicit STAGE it occurred in
(tracked via the `Stage` values below, set BEFORE the operation
belonging to that stage runs, never inferred after the fact from
exception class or context-manager entry alone): PRECHECK (environment
construction, platform authority, clean-tree, evidence-destination
reservation, validation checks) -> PRECHECK_FAILURE; STARTUP
(dual_deployment() setup, before a scenario body runs) ->
STARTUP_FAILURE; SCENARIO (a scenario body) -> SCENARIO_FAILURE;
CLEANUP (teardown-only) -> CLEANUP_FAILURE; FINALIZE (typed proof/
metadata construction, evidence rendering, secret scanning,
transactional publication) -> also CLEANUP_FAILURE/exit 5, since
FINALIZE is an internal stage with no exit code of its own.

Failure precedence: a single scenario's body AND its own cleanup can
both fail -- the body's own classification (STARTUP_FAILURE or
SCENARIO_FAILURE, whichever applies) remains primary; the cleanup
failure is still recorded separately (evidence.cleanup_failure), never
dropped. Across the whole run, the WORST category observed among all
attempted scenarios (STARTUP_FAILURE > SCENARIO_FAILURE >
CLEANUP_FAILURE) becomes `RunState.primary_failure`'s category. A
cleanup/finalization failure or a retained execution resource or a
failed/skipped publication NEVER downgrades or replaces an already-
worse primary failure; each only prevents a would-be PASS.

Evidence security boundary: raw exception text (`str(exc)`/`repr(exc)`)
is NEVER serialized into evidence, console output, or any retained
artifact. Only a FailureRecord built from an explicit safe template
(category + stage + the exception's TYPE NAME only) is ever recorded --
see _sanitize_exception(). Known long-lived per-run secrets (Node
bearer secret, DB admin password, private key PEM) are still registered
with SecretRegistry as defense in depth for any OTHER text that does
get rendered (proofs, validation status, etc).
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import django
import psycopg

from scripts.release_gate import scenarios as scenario_module
from scripts.release_gate.database import GateDatabaseNames, drop_database
from scripts.release_gate.e2e_support import db_admin_from_env
from scripts.release_gate.e2e_support import dual_deployment as real_dual_deployment
from scripts.release_gate.environment import (
    GateEnvironment,
    PreflightError,
    assert_linux_platform,
    build_gate_environment,
    compute_authority,
)
from scripts.release_gate.evidence import (
    EXIT_CODE,
    SCHEMA_VERSION,
    FailureRecord,
    InvalidProofError,
    ReleaseEvidence,
    ScenarioResult,
    SecretRegistry,
    StagingCleanupError,
    UnsupportedEvidenceValueError,
    build_typed_assertions,
    build_typed_proofs,
    deep_freeze,
    publish_evidence,
    validate_exact_type,
    validate_for_pass,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

CENTRAL_SETTINGS_MODULE = "linuxlab.settings.central"
NODE_SETTINGS_MODULE = "linuxlab.settings.node"


class Stage:
    """The explicit gate-run lifecycle state machine (Task 15 Task 11
    corrective v7, Section 5). A stage is set on `RunState.stage` BEFORE
    the operation belonging to it runs, never inferred after the fact
    from context-manager entry or exception class alone."""

    PRECHECK = "PRECHECK"
    STARTUP = "STARTUP"
    SCENARIO = "SCENARIO"
    CLEANUP = "CLEANUP"
    FINALIZE = "FINALIZE"
    DONE = "DONE"


class Decision:
    """Task 15 Task 11 corrective v7, Section 3/4: the closed set of
    values `RunState.decision` may ever hold. Values are exactly the
    evidence.EXIT_CODE category names so a decision maps directly to an
    exit code; "UNDECIDED" is the only value that does NOT (there is no
    exit code for "not yet decided" -- by the time a GateRunResult is
    returned, `RunState.decision` has always been resolved to one of the
    other five)."""

    UNDECIDED = "UNDECIDED"
    PASS = "PASS"
    PRECHECK_FAILURE = "PRECHECK_FAILURE"
    STARTUP_FAILURE = "STARTUP_FAILURE"
    SCENARIO_FAILURE = "SCENARIO_FAILURE"
    CLEANUP_FAILURE = "CLEANUP_FAILURE"


class PublicationOutcome:
    """Task 15 Task 11 corrective v7, Section 18: the closed state of
    evidence publication. PASS requires exactly PUBLISHED (Section 19) --
    a missing destination (NOT_ATTEMPTED) or a publish failure (FAILED)
    both make PASS impossible, regardless of how well-formed the
    candidate otherwise was."""

    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class ResourceKind:
    """Task 15 Task 11 corrective v7, Section 10/11 (extended by
    corrective v10, Section 12/15): the closed set of resource
    categories the ledger tracks. Restore Central/Node databases are
    first-class members here -- never inferred only from a cleanup-
    exception's action name (Section 11: "Restore DBs are first-class
    resources, not inferred from cleanup error names"). TEMP_LLPKG/
    TEMP_DUMP/RAW_RUNTIME_LOG (corrective v10, Section 15) give the
    generated `.llpkg` package, backup/restore dump files, and owned
    Gunicorn runtime logs individual live ownership entries while they
    physically exist, even though their physical disposal remains
    delegated to the enclosing workspace's own cleanup (Section 16-18:
    "do not require redundant per-file delete operations merely for
    ledger bookkeeping")."""

    CENTRAL_DB = "central_db"
    NODE_DB = "node_db"
    RESTORE_CENTRAL_DB = "restore_central_db"
    RESTORE_NODE_DB = "restore_node_db"
    WORKSPACE = "workspace"
    OUTER_ROOT = "outer_root"
    STAGING_DIR = "staging_dir"
    TEMP_LLPKG = "temp_llpkg"
    TEMP_DUMP = "temp_dump"
    RAW_RUNTIME_LOG = "raw_runtime_log"


class ResourceClass:
    """Task 15 Task 11 corrective v10, Section 12: a closed resource-
    class taxonomy separating genuine EXECUTION residue (databases,
    workspace, and the workspace-contained artifacts within it) from
    PUBLICATION residue (the evidence staging directory) and
    INFRASTRUCTURE residue (the run's own outer temp root) -- so a
    retained INFRASTRUCTURE/PUBLICATION-only resource is never
    misreported as retained EXECUTION state (Section 13/14: "this
    separates semantic retention reporting from overall release-gate
    success" -- PASS remains blocked through the co-occurring cleanup/
    publication failure that every real code path leaving one of these
    retained already records, never through this field)."""

    EXECUTION = "EXECUTION"
    PUBLICATION = "PUBLICATION"
    INFRASTRUCTURE = "INFRASTRUCTURE"


# Task 15 Task 11 corrective v10, Section 12: every ResourceKind maps to
# EXACTLY one ResourceClass. Exact enum names/mapping shape may differ
# from the design spec's own illustrative suggestion (Section 12 says
# so explicitly) -- this is the smallest closed mapping that satisfies
# it: every workspace-contained resource (including the three new
# artifact kinds) is EXECUTION, the evidence staging directory is
# PUBLICATION, and the run's own outer temp root is INFRASTRUCTURE.
_RESOURCE_CLASS = {
    ResourceKind.CENTRAL_DB: ResourceClass.EXECUTION,
    ResourceKind.NODE_DB: ResourceClass.EXECUTION,
    ResourceKind.RESTORE_CENTRAL_DB: ResourceClass.EXECUTION,
    ResourceKind.RESTORE_NODE_DB: ResourceClass.EXECUTION,
    ResourceKind.WORKSPACE: ResourceClass.EXECUTION,
    ResourceKind.TEMP_LLPKG: ResourceClass.EXECUTION,
    ResourceKind.TEMP_DUMP: ResourceClass.EXECUTION,
    ResourceKind.RAW_RUNTIME_LOG: ResourceClass.EXECUTION,
    ResourceKind.STAGING_DIR: ResourceClass.PUBLICATION,
    ResourceKind.OUTER_ROOT: ResourceClass.INFRASTRUCTURE,
}


class ResourceOwnershipError(Exception):
    """Task 15 Task 11 corrective v12, Section 18: raised when `acquire()`
    is called for a `(kind, safe_id)` pair that is already ACTIVELY
    owned. Before this corrective, a duplicate acquire was silently
    accepted (a Python `set.add()` of an already-equal element is a
    no-op that keeps the ORIGINAL stored element, discarding whatever
    new parent metadata the second call carried) -- a caller could
    believe a resource's parent had just been updated when it had not
    been. Rejecting outright makes this fail loudly instead. A resource
    that has been genuinely released is free to be re-acquired -- this
    only ever fires against a currently-active resource."""


@dataclass(frozen=True)
class ResourceRef:
    """One concrete owned resource: its category plus a SAFE identity
    (a database name or filesystem path -- never a credential, DSN,
    private-key, or payload; Section 17).

    `parent_kind`/`parent_safe_id` (Task 15 Task 11 corrective v11,
    Section 13): the EXPLICIT, STRUCTURAL parent this resource was
    registered under at acquisition time (or relocated to later --
    see `ResourceLedger.relocate()`), never inferred from a path-string
    prefix (Section 14 bans that outright: `/tmp/run` and
    `/tmp/run-outside/file.dump` share a string prefix but are not
    parent/child). `compare=False` -- a resource's IDENTITY (used by
    every existing `acquire()`/`release()`/`contains()` call site
    throughout this codebase, none of which know or care about parent
    info) remains exactly `(kind, safe_id)`, exactly as before this
    corrective; parent info is carried metadata, not part of equality/
    hashing, so an existing bare `ResourceRef(kind=k, safe_id=s)` probe
    still matches a stored entry that happens to carry parent fields.

    `resource_id`/`parent_resource_id` (Task 15 Task 11 corrective v12,
    Section 14-19): a STABLE, opaque identity assigned once at
    `acquire()` and preserved across `relocate()` -- unlike `(kind,
    safe_id)`, which changes the instant a resource's locator changes,
    `resource_id` never does. `parent_resource_id` is the acquiring/
    relocating call's best-effort resolution of `parent_kind`/
    `parent_safe_id` to whatever resource_id was actively registered
    under that (kind, safe_id) pair at that moment (`None` if nothing
    matched -- e.g. a caller that registers structural parent-info
    metadata without the parent itself ever being a real ledger entry,
    same as `parent_kind`/`parent_safe_id` always tolerated). Also
    `compare=False`, for the same reason as the two fields above."""

    kind: str
    safe_id: str
    parent_kind: str | None = field(default=None, compare=False)
    parent_safe_id: str | None = field(default=None, compare=False)
    resource_id: str = field(default="", compare=False)
    parent_resource_id: str | None = field(default=None, compare=False)


class ResourceLedger:
    """Task 15 Task 11 corrective v7, Section 10 (hardened by corrective
    v12, Section 14-23): the ONE authoritative resource-ownership
    ledger. A resource enters the ledger when ownership acquisition is
    attempted and leaves it ONLY once cleanup of that EXACT resource has
    been verified successful (Section 12) -- never optimistically
    assumed clean. `RunState.resources` is the only instance of this
    that exists for a given run.

    Internally keyed by the stable `resource_id` corrective v12
    introduces (`_records`), with `_locator_index` as a secondary,
    non-authoritative `(kind, safe_id) -> resource_id` lookup -- every
    existing call site in this codebase (and every v7-v11 regression
    test) addresses resources by `(kind, safe_id)` exactly as before;
    this class resolves that to the stable record underneath without
    requiring any caller to change."""

    def __init__(self):
        self._records: dict[str, ResourceRef] = {}
        self._locator_index: dict[tuple[str, str], str] = {}

    def acquire(self, kind: str, safe_id: str, *, parent_kind: str | None = None, parent_safe_id: str | None = None) -> str:
        """Registers a new resource and returns its stable `resource_id`
        (Task 15 Task 11 corrective v12, Section 17). Raises
        `ResourceOwnershipError` if `(kind, safe_id)` is already
        actively owned (Section 18) -- never silently keeps the
        original entry's now-stale parent metadata."""
        locator_key = (kind, safe_id)
        if locator_key in self._locator_index:
            raise ResourceOwnershipError(
                f"resource already actively owned for kind={kind!r} safe_id={safe_id!r} -- "
                "duplicate acquisition is rejected rather than silently preserving or "
                "updating parent metadata"
            )
        parent_ref = (
            self._locator_index.get((parent_kind, parent_safe_id))
            if parent_kind is not None and parent_safe_id is not None
            else None
        )
        resource_id = uuid.uuid4().hex
        ref = ResourceRef(
            kind=kind,
            safe_id=safe_id,
            parent_kind=parent_kind,
            parent_safe_id=parent_safe_id,
            resource_id=resource_id,
            parent_resource_id=parent_ref,
        )
        self._records[resource_id] = ref
        self._locator_index[locator_key] = resource_id
        return resource_id

    def release(self, kind: str, safe_id: str) -> None:
        resource_id = self._locator_index.pop((kind, safe_id), None)
        if resource_id is not None:
            self._records.pop(resource_id, None)

    def contains(self, kind: str, safe_id: str) -> bool:
        return (kind, safe_id) in self._locator_index

    def get(self, resource_id: str) -> ResourceRef | None:
        """Task 15 Task 11 corrective v12, Section 16/31: stable lookup
        by `resource_id` -- returns `None` once that exact resource has
        been released (there is deliberately no way to look up a
        released resource_id; a released resource is simply gone, same
        as `contains()` already treats a released `(kind, safe_id))`."""
        return self._records.get(resource_id)

    def set_parent(self, resource_id: str, parent_resource_id: str | None) -> None:
        """Task 15 Task 11 corrective v12, Section 18: explicit,
        deterministic parent metadata update through a dedicated API --
        never as a side effect of a second `acquire()` call on the same
        locator (which `acquire()` now rejects outright)."""
        ref = self._records.get(resource_id)
        if ref is None:
            raise ResourceOwnershipError(f"no active resource for resource_id={resource_id!r}")
        self._records[resource_id] = replace(ref, parent_resource_id=parent_resource_id)

    def relocate(
        self,
        kind: str,
        old_safe_id: str,
        new_safe_id: str,
        *,
        parent_kind: str | None = None,
        parent_safe_id: str | None = None,
    ) -> None:
        """Task 15 Task 11 corrective v11, Section 19 (hardened by
        corrective v12, Section 20): updates the ledger explicitly when
        gate code moves/renames an owned resource to a new physical
        locator -- the resource must never move physically while the
        ledger continues to point only at its stale old locator.

        Task 15 Task 11 corrective v12, Section 20: this is now an
        in-place update of the existing record -- never `release(old) +
        acquire(new)` -- so the resource's stable `resource_id` (and
        therefore any child's already-captured `parent_resource_id`
        pointing at it) survives the move unchanged; only `safe_id`
        (and, explicitly, `parent_kind`/`parent_safe_id`/
        `parent_resource_id`) are updated. Parent defaults to none, so a
        resource relocated OUTSIDE its original parent (Section 21's
        own adversarial case) is correctly no longer that parent's
        child at all unless a new parent is explicitly given."""
        old_locator_key = (kind, old_safe_id)
        resource_id = self._locator_index.pop(old_locator_key, None)
        if resource_id is None:
            raise ResourceOwnershipError(
                f"cannot relocate an unowned resource: kind={kind!r} safe_id={old_safe_id!r}"
            )
        old_ref = self._records[resource_id]
        parent_ref = (
            self._locator_index.get((parent_kind, parent_safe_id))
            if parent_kind is not None and parent_safe_id is not None
            else None
        )
        new_ref = replace(
            old_ref,
            safe_id=new_safe_id,
            parent_kind=parent_kind,
            parent_safe_id=parent_safe_id,
            parent_resource_id=parent_ref,
        )
        self._records[resource_id] = new_ref
        self._locator_index[(kind, new_safe_id)] = resource_id

    def children_of(self, parent_kind: str, parent_safe_id: str) -> tuple:
        """Task 15 Task 11 corrective v11, Section 13/16: every
        currently-owned resource whose recorded parent is EXACTLY this
        (kind, safe_id) pair -- structural identity, never a path-
        string prefix match."""
        return tuple(
            sorted(
                (
                    ref
                    for ref in self._records.values()
                    if ref.parent_kind == parent_kind and ref.parent_safe_id == parent_safe_id
                ),
                key=lambda ref: (ref.kind, ref.safe_id),
            )
        )

    def reconcile_verified_parent_removal(self, parent_kind: str, parent_safe_id: str) -> None:
        """Task 15 Task 11 corrective v11, Section 16/17/20/21: called
        ONLY after the caller has already independently verified that
        the parent resource's own physical removal succeeded. Releases
        each registered child of this EXACT parent (matched structurally
        via `children_of()`, never a path-string prefix) whose OWN
        locator is independently re-confirmed absent here -- recursing
        into any further descendants of a released child (Section 15's
        WORKSPACE -> TEMP_LLPKG/TEMP_DUMP/RAW_RUNTIME_LOG nesting, and
        the deeper OUTER_ROOT -> WORKSPACE -> ... chain), so a
        grandchild is released once its own parent chain up to the
        removed root is confirmed gone. A child whose locator still
        physically exists (Section 19/21: it was relocated outside the
        parent, or the parent's own removal did not actually reach it)
        remains owned, together with everything still registered
        beneath IT -- never a blind release of an entire subtree merely
        because the top-level parent path was removed (Section 17: "no
        blind descendant release").

        A resource that carries no matching structural parent at all
        (e.g. one a test/caller registered directly without ever going
        through the real acquisition path that would have set one) is
        still reconciled here via a SECONDARY, genuine-path-containment
        check (Section 14: "If path containment is ever needed for
        verification, use true normalized path semantics... and only as
        a secondary check, not as ownership source-of-truth") --
        `Path.resolve()` + `relative_to()`, never a string-prefix match.
        This is never used AS a substitute for structural identity on
        its own: it only ever releases a resource that is BOTH genuinely
        (not merely string-) contained within the removed parent's own
        locator AND independently confirmed physically absent, exactly
        the same two-part proof required of an explicit child. A same-
        prefix SIBLING such as `/tmp/run-outside/file.dump` against
        parent `/tmp/run` fails the genuine-containment test (`relative_
        to()` raises `ValueError` -- it is not actually a subpath) and
        so is never touched by this fallback either."""
        explicit_children = self.children_of(parent_kind, parent_safe_id)
        for child in explicit_children:
            if not os.path.exists(child.safe_id):
                self.release(child.kind, child.safe_id)
                self.reconcile_verified_parent_removal(child.kind, child.safe_id)

        try:
            resolved_parent = Path(parent_safe_id).resolve()
        except (OSError, ValueError):
            return
        explicit_child_keys = {(child.kind, child.safe_id) for child in explicit_children}
        for ref in self.all_retained_resources():
            if (ref.kind, ref.safe_id) in explicit_child_keys:
                continue
            try:
                Path(ref.safe_id).resolve().relative_to(resolved_parent)
            except (OSError, ValueError):
                continue
            if not os.path.exists(ref.safe_id):
                self.release(ref.kind, ref.safe_id)
                self.reconcile_verified_parent_removal(ref.kind, ref.safe_id)

    def retained_execution_resources(self) -> tuple:
        """Task 15 Task 11 corrective v10, Section 13: every resource
        still owned WHOSE KIND CLASSIFIES AS EXECUTION, in a fixed
        (kind, safe_id) order -- never sensitive to insertion order,
        since a `set` carries none worth relying on. A retained
        PUBLICATION (evidence staging) or INFRASTRUCTURE (outer root)
        resource is deliberately excluded here (Section 13/14): it
        remains fully tracked -- see `all_retained_resources()` -- and
        can still block PASS through its own cleanup/publication
        failure state, but must never be conflated with genuine
        execution-resource retention."""
        return tuple(
            sorted(
                (ref for ref in self._records.values() if _RESOURCE_CLASS.get(ref.kind) == ResourceClass.EXECUTION),
                key=lambda ref: (ref.kind, ref.safe_id),
            )
        )

    def all_retained_resources(self) -> tuple:
        """Task 15 Task 11 corrective v10, Section 14: the FULL retained
        set, unfiltered by resource class -- for audit/diagnostic use
        and for cross-resource reconciliation (Section 19: releasing
        every still-owned ref contained within a verified-removed
        directory, regardless of its class) rather than for PASS-gating
        logic, which must always go through `retained_execution_
        resources()` instead."""
        return tuple(sorted(self._records.values(), key=lambda ref: (ref.kind, ref.safe_id)))

    def is_empty(self) -> bool:
        return not self._records


@dataclass
class RunState:
    """Task 15 Task 11 corrective v7, Section 3: the ONE authoritative
    terminal-truth object for a gate run. Lifecycle stage, the final
    decision, every failure category, resource ownership, and
    publication outcome are all read from and written to this single
    object -- nothing here is duplicated into a second, independently-
    updated local variable elsewhere. Starts fully UNDECIDED/empty
    (Section 4: "no initial PASS")."""

    stage: str = Stage.PRECHECK
    decision: str = Decision.UNDECIDED
    primary_failure: FailureRecord | None = None
    cleanup_failure: FailureRecord | None = None
    finalization_failure: FailureRecord | None = None
    resources: ResourceLedger = field(default_factory=ResourceLedger)
    publication_outcome: str = PublicationOutcome.NOT_ATTEMPTED


# Worst-first, for folding each scenario's own PRIMARY failure category
# into `RunState.primary_failure` -- see _record_primary_failure().
# Cleanup/finalization failures are never folded against this table:
# they are always secondary (Section 6), tracked as simple "did this
# happen" facts on RunState instead.
_PRIMARY_SEVERITY = {"CLEANUP_FAILURE": 1, "SCENARIO_FAILURE": 2, "STARTUP_FAILURE": 3}

# Task 15 Task 11 corrective v9, Section 14/23: the OLD
# `_CLEANUP_ACTION_TO_RESOURCE_KIND` / `_ALL_SCENARIO_RESOURCE_KINDS` /
# `_reconcile_scenario_resources()` retroactive-inference mechanism
# (pattern-matching a cleanup exception's ACTION NAME against a fixed
# table, only AFTER a scenario finished, to decide central_db/node_db/
# workspace ownership) has been REMOVED entirely -- it is exactly the
# anti-pattern spec Section 23 forbids ("ownership must not depend on
# cleanup-error names"). Ownership for these three kinds is now
# established the INSTANT each physical resource is created, inside
# scripts/release_gate/e2e_support.py's own dual_deployment() (given
# the shared `state.resources` ledger below), and released ONLY
# immediately after that exact resource's OWN disposal action verifiably
# succeeds -- see dual_deployment()'s own cleanup-action closures. No
# post-hoc reconciliation step is needed here any more: by the time
# dual_deployment()'s `finally` block (and therefore this function's own
# `with` statement) has finished, the ledger already reflects verified
# reality for central_db/node_db/workspace, exactly as it always has for
# the two restore databases (Section 9 of corrective v8).


def _record_primary_failure(state: RunState, category: str, record: FailureRecord) -> None:
    """Folds one scenario's own PRIMARY failure into `RunState.
    primary_failure` -- replaced only by a WORSE category (Section 6:
    the worst category observed across the whole run is what remains
    authoritative), never by an equal-or-lesser one, and never cleared
    once set."""
    if state.primary_failure is None or _PRIMARY_SEVERITY[category] > _PRIMARY_SEVERITY[state.primary_failure.category]:
        state.primary_failure = record


def _record_cleanup_failure(state: RunState, record: FailureRecord) -> None:
    """Cleanup failures are always secondary (Section 6) -- aggregated
    into ONE record on RunState (first one wins its own fields; a
    second and later one is folded into a combined safe_message) rather
    than competing against primary_failure by severity."""
    if state.cleanup_failure is None:
        state.cleanup_failure = record
    else:
        combined = f"{state.cleanup_failure.safe_message}; {record.safe_message}"
        state.cleanup_failure = FailureRecord(
            category="CLEANUP_FAILURE", stage="multiple", code="CleanupAggregateError", safe_message=combined
        )


def _would_pass_ignoring_publication(state: RunState) -> bool:
    """Every _derive_decision() condition EXCEPT publication outcome --
    used only to predict, before publication is attempted, what would
    get written into the published pair if publication goes on to
    succeed (Section 21). Never used as a substitute for
    _derive_decision() itself, which alone is authoritative for the
    value this function actually returns to the caller."""
    return (
        state.primary_failure is None
        and state.cleanup_failure is None
        and state.finalization_failure is None
        and not state.resources.retained_execution_resources()
    )


def _derive_decision(state: RunState) -> str:
    """Task 15 Task 11 corrective v7, Section 28: the ONE authoritative
    PASS/fail derivation for the value _finalize() actually returns to
    its caller, consulted after publication has been attempted. No
    branch here ever starts from an assumed PASS -- PASS is the single
    `else` reached only when every other condition has already been
    ruled out, including publication actually having succeeded (Section
    18-20: "PASS is impossible" without it)."""
    if state.primary_failure is not None:
        return state.primary_failure.category
    if state.cleanup_failure is not None:
        return Decision.CLEANUP_FAILURE
    if state.finalization_failure is not None:
        return Decision.CLEANUP_FAILURE
    if state.resources.retained_execution_resources():
        return Decision.CLEANUP_FAILURE
    if state.publication_outcome != PublicationOutcome.PUBLISHED:
        return Decision.CLEANUP_FAILURE
    return Decision.PASS


def _emergency_failure_record(category: str) -> FailureRecord:
    """Task 15 Task 11 corrective v7, Section 7: built from ONLY fixed,
    compile-time-known strings -- no exception text, no timestamp, no
    dynamic content of any kind."""
    return FailureRecord(
        category=category,
        stage=category.lower(),
        code="EmergencyTerminalFailure",
        safe_message="an internal release-gate failure could not be safely detailed",
    )


# Task 15 Task 11 corrective v7, Section 7: fully prebuilt, immutable-
# by-convention emergency terminal results -- constructed ONCE, at
# import time, from nothing but fixed literals. The last-resort
# terminal path (_select_emergency_result()) returns one of these
# directly; it never constructs a ReleaseEvidence, never calls the
# timestamp generator, renderer, sanitizer, or publisher, so a
# PERSISTENT failure in any of those (one that recurs on every call, not
# merely the first) cannot defeat this path the way it could defeat a
# dynamically-(re)built fallback.
_EMERGENCY_RESULTS = {
    category: ReleaseEvidence  # placeholder overwritten immediately below; see _build_emergency_results()
    for category in ("PRECHECK_FAILURE", "STARTUP_FAILURE", "SCENARIO_FAILURE", "CLEANUP_FAILURE")
}


def _select_emergency_result(state: RunState) -> "GateRunResult":
    """Task 15 Task 11 corrective v7, Section 8: selects a PREBUILT
    result -- never constructs one. Selection order: (1) an already-
    recorded primary failure's own category always wins, so a scenario's
    real exit code (e.g. 4) survives even a total FINALIZE collapse
    (Section 8's own worked example); (2) otherwise classify from
    `RunState.stage` alone."""
    if state.primary_failure is not None:
        category = state.primary_failure.category
    else:
        stage_to_category = {
            Stage.PRECHECK: "PRECHECK_FAILURE",
            Stage.STARTUP: "STARTUP_FAILURE",
            Stage.SCENARIO: "SCENARIO_FAILURE",
            Stage.CLEANUP: "CLEANUP_FAILURE",
            Stage.FINALIZE: "CLEANUP_FAILURE",
            Stage.DONE: "CLEANUP_FAILURE",
        }
        category = stage_to_category.get(state.stage, "CLEANUP_FAILURE")
    # Written back onto the ONE shared RunState (Section 3/4) even on
    # this last-resort path -- `state.decision`/`state.stage` are never
    # left stale once a safe terminal result has been determined.
    state.decision = category
    state.stage = Stage.DONE
    return _EMERGENCY_RESULTS[category]


# Short, DB-identifier-safe codes for deriving each scenario's own CHILD
# run_id from the outer RunContext's run_id (Section 4: "the same
# run_id must drive... every ephemeral resource").
_SCENARIO_RUN_CODES = {
    "happy_path": "hp",
    "n1_tampered_replacement": "n1",
    "n2_revoked_credential": "n2",
    "n3_ack_lost_real_proxy": "n3",
    "n4_central_unavailable": "n4",
    "n5_node_restart": "n5",
    "backup_restore_smoke": "br",
}

DEFAULT_SCENARIO_SPECS = (
    ("happy_path", scenario_module.run_happy_path),
    ("n1_tampered_replacement", scenario_module.run_n1_tampered_replacement),
    ("n2_revoked_credential", scenario_module.run_n2_revoked_credential),
    ("n3_ack_lost_real_proxy", scenario_module.run_n3_ack_lost_real_proxy),
    ("n4_central_unavailable", scenario_module.run_n4_central_unavailable),
    ("n5_node_restart", scenario_module.run_n5_node_restart),
    ("backup_restore_smoke", scenario_module.run_backup_restore_smoke),
)


@dataclass(frozen=True)
class GateRunResult:
    """Task 15 Task 11 corrective v10, Section 4-6: the entire returned
    terminal result graph must be immutable, not only `ReleaseEvidence`
    nested inside it -- a cached emergency result (see
    `_EMERGENCY_RESULTS` below) is a module-level global returned BY
    REFERENCE on every call that hits that path, so if this dataclass
    were mutable, a caller mutating one field (e.g. `overall = "PASS"`)
    would PERMANENTLY corrupt that cached global for every subsequent
    run for the rest of the process's lifetime. Frozen at the top level
    (blocking field reassignment) AND with `terminal_failure` deep-
    frozen in `__post_init__` (blocking nested item mutation) closes
    both vectors."""

    overall: str
    exit_code: int
    evidence: ReleaseEvidence | None
    evidence_dir: Path | None
    json_path: Path | None
    markdown_path: Path | None
    # Populated ONLY by the last-resort emergency terminal path, when
    # the normal FINALIZE machinery itself failed -- a safe
    # FailureRecord.to_dict() describing that failure, since `evidence`
    # (and therefore `evidence.finalization_failure`) is unavailable in
    # that case. None on every ordinary path, where the usual
    # evidence.*_failure fields already carry this information.
    terminal_failure: dict | None = None
    # The final lifecycle stage this run actually reached -- an
    # OBSERVABLE proof that FINALIZE and DONE are real transitions, not
    # merely declared Stage values nothing ever sets. Always Stage.DONE
    # once a terminal decision has been safely determined, on every path
    # (PASS, any ordinary failure category, or the emergency path
    # alike) -- there is no other value a caller ever observes.
    stage: str = Stage.DONE

    def __post_init__(self):
        # Task 15 Task 11 corrective v12, Section 6: GateRunResult seals
        # its OWN top-level terminal scalars independently of
        # ReleaseEvidence -- it must not trust that a nested
        # ReleaseEvidence (when present) is sufficient, since `evidence`
        # is None on every emergency/no-destination path, and `overall`/
        # `exit_code`/`stage` are separately-constructible fields even
        # when it is not.
        validate_exact_type(self.overall, str, field_name="GateRunResult.overall")
        validate_exact_type(self.exit_code, int, field_name="GateRunResult.exit_code")
        validate_exact_type(self.stage, str, field_name="GateRunResult.stage")
        for path_field_name in ("evidence_dir", "json_path", "markdown_path"):
            value = getattr(self, path_field_name)
            # `isinstance`, never `validate_exact_type()`'s exact-type
            # match, is deliberate here: `pathlib.Path` is a factory --
            # `Path("/x")` actually returns a `PosixPath`/`WindowsPath`
            # instance, so `type(value) is Path` is False for every real
            # Path this codebase ever constructs. These three fields are
            # plain runtime file-reference metadata, never part of the
            # published terminal evidence algebra itself (Section 4 does
            # not include Path at all), so the concern that motivates
            # exact-type matching elsewhere (a scalar subclass silently
            # carrying hidden behavior) does not apply the same way --
            # what must be rejected is a non-Path stand-in (a bare str,
            # an arbitrary object), which `isinstance` already does.
            if value is not None and not isinstance(value, Path):
                raise UnsupportedEvidenceValueError(
                    f"GateRunResult.{path_field_name} must be a pathlib.Path or None, "
                    f"got {type(value).__name__!r}"
                )
        # object.__setattr__ is required here -- the dataclass itself is
        # frozen, so ordinary attribute assignment (even from inside
        # __post_init__) would raise dataclasses.FrozenInstanceError.
        # `evidence` needs no special handling here: it is already
        # deeply immutable via ReleaseEvidence's own __post_init__.
        object.__setattr__(self, "terminal_failure", deep_freeze(self.terminal_failure))


def _build_emergency_results() -> None:
    for category in list(_EMERGENCY_RESULTS):
        record = _emergency_failure_record(category)
        _EMERGENCY_RESULTS[category] = GateRunResult(
            overall=category,
            exit_code=EXIT_CODE[category],
            evidence=None,
            evidence_dir=None,
            json_path=None,
            markdown_path=None,
            terminal_failure=record.to_dict(),
            stage=Stage.DONE,
        )


_build_emergency_results()


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _child_run_id(outer_run_id: str, scenario_name: str) -> str:
    code = _SCENARIO_RUN_CODES.get(scenario_name, scenario_name[:2])
    return f"{outer_run_id}_{code}"


def _sanitize_exception(exc: BaseException, stage: str, category: str, secrets: SecretRegistry) -> FailureRecord:
    """The ONLY place an exception is turned into evidence content.
    `code` (the exception's class name) is the sole exception-derived
    fact ever used -- never `str(exc)`/`repr(exc)`, which could echo
    back arbitrary/sensitive content an assertion message happened to
    embed. `safe_message` is a fixed template over already-safe
    strings (category/stage/code), then redacted as defense in depth
    even though none of its inputs should ever contain a registered
    secret by construction."""
    code = type(exc).__name__
    safe_message = secrets.redact_text(f"{code} raised during {stage}")
    return FailureRecord(category=category, stage=stage, code=code, safe_message=safe_message)


def _register_deployment_secrets(deployment, secrets: SecretRegistry) -> None:
    node_secret = getattr(deployment, "node_secret", None)
    if node_secret:
        secrets.register(node_secret)
    private_key_path = getattr(deployment, "private_key_path", None)
    if private_key_path is not None:
        path = Path(private_key_path)
        if path.exists():
            secrets.register(path.read_text())


def _dispose_restore_databases(state: RunState, gate_names: GateDatabaseNames, secrets: SecretRegistry) -> None:
    """Task 15 Task 11 corrective v8, Section 14-16: restore Central/
    Node database disposal ALWAYS happens here, under Stage.CLEANUP,
    regardless of how the scenario body itself concluded -- never
    inside the scenario's own body (which would misclassify a drop
    failure as an ordinary scenario failure while cleanup_status stayed
    clean). A safe, cheap no-op for every scenario except
    backup_restore_smoke, the only one that ever acquires these two
    ledger entries (via the shared `deployment.resource_ledger`,
    scenarios.run_backup_restore_smoke()'s own doing). Both members are
    always attempted, even if the first raises, matching the same
    invariant already enforced for dual_deployment()'s own central/node
    database cleanup."""
    db_admin = db_admin_from_env()
    for member, kind in (
        ("restore_node", ResourceKind.RESTORE_NODE_DB),
        ("restore_central", ResourceKind.RESTORE_CENTRAL_DB),
    ):
        safe_id = getattr(gate_names, member)
        if not state.resources.contains(kind, safe_id):
            continue
        try:
            drop_database(db_admin, gate_names, member)
        except Exception as exc:  # noqa: BLE001 -- classified as a cleanup failure below, never silently dropped
            _record_cleanup_failure(
                state, _sanitize_exception(exc, "restore_database_cleanup", "CLEANUP_FAILURE", secrets)
            )
        else:
            state.resources.release(kind, safe_id)


def _run_one_scenario(
    state: RunState, outer_run_id, name, scenario_func, scenario_tmp_dir, dual_deployment_factory, secrets
):
    """Runs exactly one scenario in its own dual_deployment(), using a
    CHILD run_id derived from `outer_run_id` (Section 4: traceable
    identity, never an unrelated random one). Mutates `state` in place
    -- records this scenario's own primary failure (if any, folded by
    severity into `state.primary_failure`), its own cleanup failure (if
    any). Central/Node database and workspace ownership (Task 15 Task
    11 corrective v9, Section 14/15) are established/released directly
    by dual_deployment_factory(...) itself (given the shared ledger
    passed into it below), never reconciled here after the fact;
    restore-database disposal is still this function's own explicit
    CLEANUP-stage responsibility (Section 15/16 of corrective v8; see
    _dispose_restore_databases()). Returns (ScenarioResult,
    detail_value_or_None).

    Explicit stage tracking (hardened by corrective v4 Section 6.4):
    `stage` starts at Stage.STARTUP and is advanced to Stage.SCENARIO
    only once dual_deployment_factory(...) has actually yielded a
    deployment -- an exception before that point is a startup failure by
    construction, never inferred from exception type. Once the scenario
    body (and typed assertion construction, Section 8) fully completes,
    `stage` is advanced ONE FURTHER to Stage.CLEANUP before the `with`
    block's own suite ends -- the only code that can still run after
    that point is `__exit__` (teardown). This means that IF
    `outcome_exception` is set below, checking `stage == Stage.CLEANUP`
    alone proves it came from teardown, for ANY exception class
    whatsoever -- never by checking whether it happens to be a
    `CleanupAggregateError` specifically. A malformed assertion value
    fails the scenario itself (never reaches Stage.CLEANUP), using the
    exact same precedence handling as any other body failure.

    The scenario body's own exception is deliberately let propagate
    NATURALLY through `dual_deployment_factory(...)`'s own `finally`
    (never caught inside the `with` block itself): it lets
    dual_deployment() correctly observe via `sys.exc_info()` that the
    run failed, and if cleanup ALSO then raises, Python's own implicit
    exception chaining sets the new exception's `__context__` to the
    original body/setup exception for free -- checked generically here
    (`__context__ is not None`), again never by testing the new
    exception's class."""
    child_run_id = _child_run_id(outer_run_id, name)
    gate_names = GateDatabaseNames.for_run(child_run_id)

    start = time.monotonic()
    # Task 15 Task 11 corrective v7, Section 3: written directly onto
    # the ONE shared `state.stage` -- never a locally-shadowed variable
    # that would leave `state` stale/decorative during this scenario's
    # own STARTUP/SCENARIO/CLEANUP transitions.
    state.stage = Stage.STARTUP
    outcome_exception = None
    detail_value = None
    assertions: dict = {}

    try:
        # Task 15 Task 11 corrective v9, Section 13/14/15: the shared
        # ledger is passed straight into the factory now, so
        # dual_deployment() (the real one) can acquire the workspace and
        # both ephemeral databases the INSTANT each physical creation
        # succeeds -- BEFORE this function ever regains control, never
        # only after the fact via the `deployment.resource_ledger = ...`
        # assignment below (kept for the SAME reason it always existed:
        # scenario code and any dual_deployment_factory implementation
        # that does not itself accept/use this kwarg -- e.g. a fake
        # factory in a fast orchestration test -- still finds a working
        # ledger reference at `deployment.resource_ledger`).
        with dual_deployment_factory(
            scenario_tmp_dir,
            run_id=child_run_id,
            resource_ledger=state.resources,
            # Task 15 Task 11 corrective v11, Section 13/15: the real
            # dual_deployment() registers its own workspace as this
            # EXACT parent's structural child (never inferred from a
            # path prefix) -- `scenario_tmp_dir.parent` is always the
            # run's own outer temp root (see the `tmp_root_path / name`
            # construction just above this function's own caller). A
            # fake/test dual_deployment_factory that does not accept or
            # use these kwargs still works unchanged (`**kwargs`).
            parent_kind=ResourceKind.OUTER_ROOT,
            parent_safe_id=str(scenario_tmp_dir.parent),
        ) as deployment:
            # STARTUP covers deployment context entry AND every post-
            # enter setup step before the scenario callable itself --
            # secret registration here included. `state.stage` only
            # advances to SCENARIO immediately before the scenario body
            # actually runs, so a failure in _register_deployment_
            # secrets() classifies as STARTUP_FAILURE, never
            # SCENARIO_FAILURE.
            _register_deployment_secrets(deployment, secrets)
            # Task 15 Task 11 corrective v8, Section 9/16: gives the
            # scenario body real-time access to the SAME shared ledger
            # -- restore-database creation (backup_restore_smoke only)
            # acquires ownership the instant it succeeds, never
            # inferred later from cleanup-exception names.
            deployment.resource_ledger = state.resources
            state.stage = Stage.SCENARIO
            detail_value = scenario_func(deployment)
            assertions = build_typed_assertions(detail_value)
            state.stage = Stage.CLEANUP
    except Exception as exc:  # noqa: BLE001 -- classified below, never silently dropped
        outcome_exception = exc

    duration_ms = (time.monotonic() - start) * 1000.0
    # Captured BEFORE restore-database disposal (below) forces
    # `state.stage` to CLEANUP unconditionally -- this is the
    # scenario's OWN concluding stage, needed to classify ITS outcome
    # correctly.
    scenario_stage = state.stage

    # Task 15 Task 11 corrective v8, Section 15/16: restore-database
    # disposal always executes under Stage.CLEANUP, regardless of how
    # the scenario body itself concluded -- never as part of the
    # scenario body's own classification.
    state.stage = Stage.CLEANUP
    _dispose_restore_databases(state, gate_names, secrets)

    if outcome_exception is None:
        return (
            ScenarioResult(name=name, status="PASS", assertions=assertions, duration_ms=duration_ms, detail=""),
            detail_value,
        )

    if scenario_stage == Stage.CLEANUP:
        # Setup and the scenario body both fully succeeded -- whatever
        # this exception's actual class is, it can only have come from
        # __exit__ (teardown).
        cleanup_record = _sanitize_exception(outcome_exception, name, "CLEANUP_FAILURE", secrets)
        _record_cleanup_failure(state, cleanup_record)
        return (
            ScenarioResult(name=name, status="FAIL", duration_ms=duration_ms, detail=cleanup_record.safe_message),
            None,
        )

    # Setup or the scenario body itself failed. If teardown ALSO then
    # failed while that original exception was unwinding, Python's own
    # implicit chaining set `__context__` to it.
    original_exception = outcome_exception.__context__
    category = "SCENARIO_FAILURE" if scenario_stage == Stage.SCENARIO else "STARTUP_FAILURE"
    if original_exception is not None:
        cleanup_record = _sanitize_exception(outcome_exception, name, "CLEANUP_FAILURE", secrets)
        _record_cleanup_failure(state, cleanup_record)
        record = _sanitize_exception(original_exception, name, category, secrets)
    else:
        record = _sanitize_exception(outcome_exception, name, category, secrets)
    _record_primary_failure(state, category, record)

    return (
        ScenarioResult(name=name, status="FAIL", duration_ms=duration_ms, detail=record.safe_message),
        None,
    )


def _run_manage_command(env: GateEnvironment, django_settings_module: str, args: list) -> subprocess.CompletedProcess:
    """Runs `manage.py <args>` for one profile. Deliberately points
    POSTGRES_DB at the universal `postgres` maintenance database --
    always present on a working PostgreSQL server, never a dev/prod/
    gate-owned database -- rather than at any real application schema,
    since `check`/`makemigrations --check` compare in-memory model
    state against migration files on disk and do not require the
    application's own tables to exist to produce an accurate result."""
    child_env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": django_settings_module,
        "POSTGRES_DB": "postgres",
        "POSTGRES_HOST": env.db_admin.host,
        "POSTGRES_PORT": env.db_admin.port,
        "POSTGRES_USER": env.db_admin.user,
        "POSTGRES_PASSWORD": env.db_admin.password,
    }
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=str(env.repo_root),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _run_validation_checks(env: GateEnvironment) -> dict:
    """Gate-level validation (Section 12): real Central/Node
    `manage.py check` and migration-drift status, run once per gate
    run, independent of any specific scenario's deployment. A PASS must
    never carry `validation = {}` -- every key here is always
    populated with a real (categorical, never raw command output)
    status string."""
    central_check = _run_manage_command(env, CENTRAL_SETTINGS_MODULE, ["check"])
    node_check = _run_manage_command(env, NODE_SETTINGS_MODULE, ["check"])
    central_drift = _run_manage_command(env, CENTRAL_SETTINGS_MODULE, ["makemigrations", "--check", "--dry-run"])
    node_drift = _run_manage_command(env, NODE_SETTINGS_MODULE, ["makemigrations", "--check", "--dry-run"])
    return {
        "clean_tree": True,  # reaching this point already required build_gate_environment()'s own clean-tree check
        "central_check": "ok" if central_check.returncode == 0 else "failed",
        "node_check": "ok" if node_check.returncode == 0 else "failed",
        "central_migration_drift": "none" if central_drift.returncode == 0 else "detected",
        "node_migration_drift": "none" if node_drift.returncode == 0 else "detected",
    }


def _validation_ok(validation: dict) -> bool:
    return (
        validation.get("central_check") == "ok"
        and validation.get("node_check") == "ok"
        and validation.get("central_migration_drift") == "none"
        and validation.get("node_migration_drift") == "none"
    )


def _environment_metadata(db_admin=None) -> dict:
    metadata = {"platform": platform.system().lower(), "python_version": platform.python_version()}
    try:
        metadata["django_version"] = django.get_version()
    except Exception:  # noqa: BLE001 -- best-effort environment metadata only
        metadata["django_version"] = "unknown"
    try:
        connect_kwargs = (
            {"host": db_admin.host, "port": db_admin.port, "user": db_admin.user, "password": db_admin.password}
            if db_admin is not None
            else {}
        )
        with psycopg.connect(dbname="postgres", connect_timeout=3, **connect_kwargs) as conn:
            metadata["postgres_version"] = str(conn.info.server_version)
    except Exception:  # noqa: BLE001 -- best-effort environment metadata only
        metadata["postgres_version"] = "unknown"
    return metadata


def _build_snapshot(
    *,
    overall: str,
    bundle_retained: bool,
    state: RunState,
    run_id,
    git_sha,
    started_at,
    finished_at,
    authority,
    environment: dict,
    validation: dict,
    scenario_results,
    proofs: dict,
    keep_on_failure: bool,
) -> ReleaseEvidence:
    """Task 15 Task 11 corrective v8, Section 4/5: builds ONE complete,
    self-consistent EvidenceV1 snapshot from CURRENT `state` plus the
    already-known facts passed in -- a pure function, never mutated
    after it returns. `overall` and `bundle_retained` are passed in
    explicitly rather than read live from `state.decision`/
    `state.publication_outcome` because the PRE-publication call needs
    a PREDICTED value (publication hasn't happened yet -- Section 8:
    "avoid circularity"), while a POST-publication call (only needed
    when that prediction turns out to disagree with reality) passes the
    real, final values instead. Every other field (resource retention,
    cleanup status, primary/cleanup/finalization failure) is read
    directly from `state`, which by either call site is already
    completely settled."""
    return ReleaseEvidence(
        schema_version=SCHEMA_VERSION,
        run={
            "run_id": run_id,
            "git_sha": git_sha,
            "started_at": started_at,
            "finished_at": finished_at,
            "authority": authority.__dict__,
        },
        environment=environment,
        profiles={"central": CENTRAL_SETTINGS_MODULE, "node": NODE_SETTINGS_MODULE},
        validation=validation,
        scenarios={result.name: result.__dict__ for result in scenario_results},
        proofs=proofs,
        retention={
            "keep_on_failure_requested": keep_on_failure,
            "execution_resources_retained": bool(state.resources.retained_execution_resources()),
            "sanitized_debug_bundle_retained": bundle_retained,
        },
        primary_failure=state.primary_failure.to_dict() if state.primary_failure is not None else None,
        cleanup_failure=state.cleanup_failure.to_dict() if state.cleanup_failure is not None else None,
        finalization_failure=state.finalization_failure.to_dict() if state.finalization_failure is not None else None,
        cleanup_status="failed" if state.cleanup_failure is not None else "clean",
        overall=overall,
        exit_code=EXIT_CODE[overall],
    )


def _predicted_overall(state: RunState) -> str:
    """The PRE-publication prediction of what `overall` will be if
    publication, which hasn't been attempted yet, goes on to succeed --
    see _build_snapshot()'s docstring. Never used as the final,
    returned decision; _derive_decision() alone is authoritative for
    that."""
    if _would_pass_ignoring_publication(state):
        return Decision.PASS
    if state.primary_failure is not None:
        return state.primary_failure.category
    return Decision.CLEANUP_FAILURE


def _finalize(
    *,
    state: RunState,
    run_id,
    git_sha,
    started_at,
    authority,
    scenario_results,
    detail_values,
    validation,
    secrets,
    evidence_dir,
    keep_on_failure,
    db_admin=None,
) -> GateRunResult:
    """Task 15 Task 11 corrective v7, Section 5/19/28: the FINALIZE
    stage. Builds typed proofs, the metadata-bearing EvidenceV1
    candidate, runs the AUTHORITATIVE evidence.validate_for_pass() (a
    direct, hardcoded call -- production code accepts no injectable
    validator/callback/hook capable of deciding PASS eligibility),
    attempts transactional publication, and ONLY THEN derives the final
    decision via _derive_decision() -- which requires
    `state.publication_outcome == PublicationOutcome.PUBLISHED` in
    addition to everything validate_for_pass() already checks (Section
    18-20: "PASS is impossible" without real, successful publication,
    including when `evidence_dir` is None and publication could never
    even be attempted).

    One outer sanitized boundary covers proof building, metadata
    building, and candidate assembly: an unexpected exception there
    normalizes to `state.finalization_failure` and falls through to a
    minimal, honest fallback candidate built from only the pieces
    already known safe. Publication is attempted independently (via
    publish_evidence()'s own broad `except Exception`) whenever
    `evidence_dir is not None`, regardless of any earlier failure --
    Section 30: normal failures may still publish sanitized evidence
    describing what happened."""
    state.stage = Stage.FINALIZE
    finished_at = _utc_now_iso()

    common_kwargs = {
        "state": state,
        "run_id": run_id,
        "git_sha": git_sha,
        "started_at": started_at,
        "finished_at": finished_at,
        "authority": authority,
        "validation": validation,
        "scenario_results": scenario_results,
        "keep_on_failure": keep_on_failure,
    }

    evidence = None
    try:
        try:
            proofs = build_typed_proofs(*detail_values.values())
        except InvalidProofError as exc:
            proofs = {}
            state.finalization_failure = _sanitize_exception(exc, "finalize_proofs", "CLEANUP_FAILURE", secrets)

        environment = _environment_metadata(db_admin)

        # Section 3/28: "no branch begins from overall=PASS" -- only
        # consulted when nothing has failed the run so far, so it can
        # only ever GATE a would-be PASS, never override an already-
        # worse primary/cleanup failure. This provisional, PASS-shaped
        # candidate exists ONLY to hand to validate_for_pass() -- it is
        # never rendered, published, or returned; if it is rejected,
        # `state.finalization_failure` is set here, and the ONE real
        # snapshot built below (from the now-fully-settled `state`)
        # naturally predicts a non-PASS category instead.
        if (
            state.finalization_failure is None
            and state.primary_failure is None
            and state.cleanup_failure is None
        ):
            provisional = _build_snapshot(
                overall=Decision.PASS, bundle_retained=True, environment=environment, proofs=proofs,
                **common_kwargs,
            )
            decision = validate_for_pass(provisional)
            if not decision.eligible:
                safe_message = secrets.redact_text("; ".join(decision.reasons))
                state.finalization_failure = FailureRecord(
                    category="CLEANUP_FAILURE",
                    stage="finalize_pass_validation",
                    code="EvidenceInvariantError",
                    safe_message=safe_message,
                )

        # Task 15 Task 11 corrective v8, Section 4/5: THE snapshot --
        # built exactly once, from `state` now that every failure it can
        # hold before publication has been fully settled. `overall`/
        # `bundle_retained` are still a PREDICTION (publication hasn't
        # been attempted yet -- Section 8 "avoid circularity"): the only
        # way this can turn out wrong is publication itself failing, and
        # publish_evidence()'s own transactional (stage-then-atomic-
        # rename) design guarantees no artifact is ever left at
        # `evidence_dir` in that case, so a persisted false "PASS" claim
        # is structurally impossible.
        evidence = _build_snapshot(
            overall=_predicted_overall(state),
            bundle_retained=evidence_dir is not None,
            environment=environment,
            proofs=proofs,
            **common_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 -- FINALIZE fail-safe boundary: normalize ANY other unexpected
        # failure while building or validating the candidate -- never
        # let raw exception content escape uncaught.
        state.finalization_failure = _sanitize_exception(exc, "finalize_build_candidate", "CLEANUP_FAILURE", secrets)
        evidence = None

    if evidence is None:
        # Candidate assembly itself never completed -- build a minimal,
        # honest fallback snapshot from only the pieces already known
        # safe before entering the boundary above.
        evidence = _build_snapshot(
            overall=_predicted_overall(state),
            bundle_retained=evidence_dir is not None,
            environment={},
            proofs={},
            **common_kwargs,
        )

    json_path = None
    markdown_path = None
    if evidence_dir is not None:
        try:
            # Task 15 Task 11 corrective v9, Section 17: the shared
            # ledger is threaded straight into publish_evidence() now,
            # which acquires the staging directory itself the instant
            # it is created (and releases it after verified disposal) --
            # never only retroactively from this except branch.
            json_path, markdown_path = publish_evidence(evidence_dir, evidence, secrets, resource_ledger=state.resources)
            state.publication_outcome = PublicationOutcome.PUBLISHED
        except Exception as exc:  # noqa: BLE001 -- publication must never crash the gate itself
            state.publication_outcome = PublicationOutcome.FAILED
            if isinstance(exc, StagingCleanupError):
                # Publication AND the subsequent staging-directory
                # cleanup BOTH failed -- genuine residue was left on
                # disk. Split into two distinct records: the staging-
                # cleanup failure itself (cleanup_failure) and the
                # ORIGINAL publication failure (preserved as
                # `__cause__`, never overwritten) as finalization_
                # failure. publish_evidence() has already acquired the
                # staging directory into the resource ledger (Section
                # 17) and, since disposal was never verified successful
                # on this exact branch, never released it either -- no
                # re-acquire is needed here any more.
                _record_cleanup_failure(
                    state,
                    _sanitize_exception(exc, "finalize_publish_staging_cleanup", "CLEANUP_FAILURE", secrets),
                )
                original_exc = exc.__cause__
                if original_exc is not None and state.finalization_failure is None:
                    state.finalization_failure = _sanitize_exception(
                        original_exc, "finalize_publish", "CLEANUP_FAILURE", secrets
                    )
            elif state.finalization_failure is None:
                state.finalization_failure = _sanitize_exception(exc, "finalize_publish", "CLEANUP_FAILURE", secrets)
    # else: no valid publication destination -- publication_outcome
    # stays NOT_ATTEMPTED (Section 18: "No destination means
    # publication cannot occur"), which alone is enough for
    # _derive_decision() below to refuse PASS.

    final_overall = _derive_decision(state)
    state.decision = final_overall
    final_bundle_retained = state.publication_outcome == PublicationOutcome.PUBLISHED

    # Task 15 Task 11 corrective v8, Section 5/6/8: if anything was
    # actually published, `evidence` at this point IS the exact object
    # publish_evidence() rendered and wrote -- it is NEVER mutated here.
    # It is only rebuilt (as a brand-new object, never re-published)
    # when reality after the publish attempt disagrees with what was
    # predicted before it (i.e. publication failed or was never
    # attempted) -- so the returned truth never contradicts whatever was
    # actually persisted, and a failed/no-destination run never silently
    # inherits a stale "would-be PASS" claim.
    if final_overall != evidence.overall or final_bundle_retained != evidence.retention["sanitized_debug_bundle_retained"]:
        evidence = _build_snapshot(
            overall=final_overall,
            bundle_retained=final_bundle_retained,
            environment=evidence.environment,
            proofs=evidence.proofs,
            **common_kwargs,
        )

    state.stage = Stage.DONE
    return GateRunResult(
        overall=evidence.overall,
        exit_code=evidence.exit_code,
        evidence=evidence,
        evidence_dir=evidence_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        stage=Stage.DONE,
    )


def _safe_finalize(*, state: RunState, **finalize_kwargs) -> GateRunResult:
    """Task 15 Task 11 corrective v7, Section 8: every call to
    _finalize() goes through here so a failure that escapes _finalize()'s
    own internal fail-safe boundary -- including a PERSISTENT one (Section
    31.1: recurs on every call, not just the first, and would therefore
    also defeat a dynamically-(re)built fallback) -- still cannot reach
    the caller raw. Selects a fully PREBUILT emergency result rather than
    constructing anything (Section 8), so an already-known scenario
    primary exit code survives even this collapse."""
    try:
        return _finalize(state=state, **finalize_kwargs)
    except Exception:  # noqa: BLE001 -- last-resort terminal fail-safe, never silently dropped, never raw
        return _select_emergency_result(state)


def run_release_gate(
    *,
    repo_root,
    keep_on_failure: bool = False,
    check_platform: bool = True,
    scenario_specs=DEFAULT_SCENARIO_SPECS,
    dual_deployment_factory=real_dual_deployment,
    environment_builder=build_gate_environment,
    run_validation=_run_validation_checks,
    tmp_root=None,
) -> GateRunResult:
    """Task 15 Task 11 corrective v7, Section 8/9: the ONE outer terminal
    fail-safe boundary for the top-level release-gate orchestration,
    outside the normal FINALIZE pipeline -- a thin wrapper around
    _run_release_gate(). Even a failure BEFORE any of that function's
    own internal try/except blocks begin (e.g. `_utc_now_iso()` itself
    raising while computing `started_at`, the very first thing it does)
    is still caught here and resolved to a PREBUILT emergency result
    (never constructed dynamically) rather than propagating raw. Never
    catches BaseException."""
    secrets = SecretRegistry()
    state = RunState()
    try:
        return _run_release_gate(
            repo_root=repo_root,
            keep_on_failure=keep_on_failure,
            check_platform=check_platform,
            scenario_specs=scenario_specs,
            dual_deployment_factory=dual_deployment_factory,
            environment_builder=environment_builder,
            run_validation=run_validation,
            tmp_root=tmp_root,
            secrets=secrets,
            state=state,
        )
    except Exception:  # noqa: BLE001 -- outermost terminal fail-safe, never silently dropped, never raw
        return _select_emergency_result(state)


def _run_release_gate(
    *,
    repo_root,
    keep_on_failure: bool,
    check_platform: bool,
    scenario_specs,
    dual_deployment_factory,
    environment_builder,
    run_validation,
    tmp_root,
    secrets: SecretRegistry,
    state: RunState,
) -> GateRunResult:
    """Runs the full release gate: preflight -> validation checks ->
    every scenario in `scenario_specs` (each in its own fresh
    dual_deployment() using a run_id DERIVED from the one RunContext
    identity established here, never a single deployment shared across
    all of them, and never an unrelated random identity either) ->
    evidence assembly -> returns the result.

    Fail-safe boundary: every stage below is wrapped so an unanticipated
    exception is still classified by the stage it occurred in and still
    produces sanitized evidence when that remains possible, rather than
    propagating a raw traceback to the caller -- and run_release_gate()
    above adds one more outer boundary for whatever might escape even
    this.

    Authority (Section 10) is computed from the ACTUAL running platform
    unconditionally, independent of `check_platform` -- a Darwin
    development run (`check_platform=False`) may still PASS
    functionally, but its evidence always self-identifies as
    non-authoritative."""
    repo_root = Path(repo_root)
    started_at = _utc_now_iso()
    authority = compute_authority(platform_name=platform.system())
    state.stage = Stage.PRECHECK

    # --- PRECHECK, part 1: platform authority + environment construction.
    # A failure here means the run's own identity was never established,
    # so there is no safe evidence destination to write to at all.
    try:
        if check_platform:
            assert_linux_platform()
        env: GateEnvironment = environment_builder(repo_root=repo_root, keep_on_failure=keep_on_failure)
    except Exception as exc:  # noqa: BLE001 -- classified below, never silently dropped, never raw
        record = _sanitize_exception(exc, "preflight", "PRECHECK_FAILURE", secrets)
        state.primary_failure = record
        return _safe_finalize(
            state=state,
            run_id="unknown",
            git_sha="unknown",
            started_at=started_at,
            authority=authority,
            scenario_results=[ScenarioResult(name="preflight", status="FAIL", detail=record.safe_message)],
            detail_values={},
            validation={},
            secrets=secrets,
            evidence_dir=None,
            keep_on_failure=keep_on_failure,
        )

    secrets.register(env.db_admin.password)

    # --- PRECHECK, part 2: evidence destination reservation + validation.
    # The run's identity IS known now (preflight failure can still
    # produce safe minimal evidence when the target is available).
    validation: dict = {}
    try:
        if env.artifact_dir.exists():
            raise PreflightError(f"release evidence directory already exists: {env.artifact_dir}")
        validation = run_validation(env)
        if not _validation_ok(validation):
            raise PreflightError("validation checks did not pass (see validation section for status)")
    except Exception as exc:  # noqa: BLE001 -- classified below, never silently dropped, never raw
        record = _sanitize_exception(exc, state.stage, "PRECHECK_FAILURE", secrets)
        state.primary_failure = record
        return _safe_finalize(
            state=state,
            run_id=env.run_id,
            git_sha=env.git_sha,
            started_at=started_at,
            authority=authority,
            scenario_results=[ScenarioResult(name=state.stage, status="FAIL", detail=record.safe_message)],
            detail_values={},
            validation=validation,
            secrets=secrets,
            evidence_dir=env.artifact_dir,
            keep_on_failure=keep_on_failure,
        )

    scenario_results: list = []
    detail_values: dict = {}

    tmp_root_path = Path(
        tempfile.mkdtemp(prefix=f"phase1-release-{env.run_id}-", dir=str(tmp_root) if tmp_root is not None else None)
    )
    # Task 15 Task 11 corrective v9, Section 16/22: acquired IMMEDIATELY
    # after physical creation succeeds -- never only retroactively, on
    # the specific branch below where its own later removal happens to
    # fail. Every scenario that runs after this point (and therefore
    # every resource IT acquires) does so while the outer root is
    # already ledger-owned.
    state.resources.acquire(ResourceKind.OUTER_ROOT, str(tmp_root_path))
    try:
        for name, scenario_func in scenario_specs:
            state.stage = name
            scenario_tmp_dir = tmp_root_path / name
            scenario_tmp_dir.mkdir(parents=True, exist_ok=True)
            result, detail_value = _run_one_scenario(
                state, env.run_id, name, scenario_func, scenario_tmp_dir, dual_deployment_factory, secrets
            )
            scenario_results.append(result)
            if detail_value:
                detail_values[name] = detail_value
    except Exception as exc:  # noqa: BLE001 -- unexpected failure OUTSIDE any single scenario's own handling
        record = _sanitize_exception(exc, state.stage, "STARTUP_FAILURE", secrets)
        _record_primary_failure(state, "STARTUP_FAILURE", record)
        scenario_results.append(ScenarioResult(name=state.stage, status="FAIL", detail=record.safe_message))
    finally:
        # Nothing is ever retained under the run's own outer temp root
        # (execution resources are never optimistically assumed clean --
        # a failure here is recorded as a genuine cleanup failure,
        # making PASS impossible, rather than silently ignored via
        # ignore_errors=True).
        state.stage = Stage.CLEANUP
        try:
            shutil.rmtree(tmp_root_path)
            # Task 15 Task 11 corrective v9, Section 16/20: verified
            # successful removal -- release the outer root itself now,
            # on this success path (never only inside the failure branch
            # below, which used to be the ONLY place this resource was
            # ever registered at all).
            state.resources.release(ResourceKind.OUTER_ROOT, str(tmp_root_path))
            # Task 15 Task 11 corrective v7, Section 15 (broadened by
            # corrective v10, Section 19; hardened by corrective v11,
            # Section 14/16/20): outer-root removal succeeding means
            # every scenario workspace nested inside it -- and every
            # workspace-contained artifact within THAT (.llpkg, dump,
            # runtime log; Section 16/18) -- is now ALSO gone, even if
            # an earlier per-scenario cleanup attempt failed to remove
            # it individually. Structural parent/child reconciliation
            # (never a path-string PREFIX match -- corrective v11,
            # Section 14 forbids that outright: two unrelated paths can
            # share a string prefix without one containing the other)
            # releases only a resource explicitly registered as this
            # exact root's descendant, and only once ITS OWN locator is
            # independently re-verified absent, cascading through
            # however many levels of nesting (OUTER_ROOT -> WORKSPACE ->
            # TEMP_LLPKG/TEMP_DUMP/RAW_RUNTIME_LOG) are actually
            # registered.
            state.resources.reconcile_verified_parent_removal(ResourceKind.OUTER_ROOT, str(tmp_root_path))
        except FileNotFoundError:
            # Already gone -- verified absence is itself verified
            # disposal (Section 20), so the outer root is released here
            # too, not left stuck retained for a resource that
            # provably no longer exists.
            state.resources.release(ResourceKind.OUTER_ROOT, str(tmp_root_path))
        # Audited cleanup covers ordinary Exception generically -- not a
        # hard-coded tuple of specific classes -- so an unanticipated
        # exception class can never escape uncaught. Never `except
        # BaseException`.
        except Exception as exc:  # noqa: BLE001 -- classified as a cleanup failure below, never silently dropped
            _record_cleanup_failure(
                state, _sanitize_exception(exc, "outer_temp_root_cleanup", "CLEANUP_FAILURE", secrets)
            )
            # Task 15 Task 11 corrective v9, Section 20: removal was NOT
            # verified successful -- the outer root, already acquired
            # above at creation time, simply stays retained (never
            # released without verified disposal). No re-acquire is
            # needed here any more; this branch no longer needs to be
            # the FIRST time the ledger hears about this resource.

    return _safe_finalize(
        state=state,
        run_id=env.run_id,
        git_sha=env.git_sha,
        started_at=started_at,
        authority=authority,
        scenario_results=scenario_results,
        detail_values=detail_values,
        validation=validation,
        secrets=secrets,
        evidence_dir=env.artifact_dir,
        keep_on_failure=keep_on_failure,
        db_admin=env.db_admin,
    )
