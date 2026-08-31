import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from linuxlab.activities.models import ActivityVersion
from linuxlab.activities.services import get_activity_version
from linuxlab.core.events import publish_event
from linuxlab.identity.models import Learner
from linuxlab.identity.services import ensure_learner
from linuxlab.learning.models import Attempt, Result
from linuxlab.learning.projections import refresh_progress
from linuxlab.runtime.context import open_pinned_context
from linuxlab.sync.enqueue import enqueue_sync_event

_GUEST_CLAIM_SOURCE = "guest_claim"
_CLAIM_ITEM_KEYS = {"activity_public_id", "activity_version"}


def _validate_provider_outcome(outcome):
    """Strict generic contract for provider evaluate() output (Task 07):
    only 'score'/'max_score' are required, and both must be exact integers
    (type(x) is int, not isinstance -- bool is an int subclass and must not
    silently pass) satisfying 0 <= score <= max_score. This mirrors the
    PostgreSQL Result constraint so malformed data is rejected at the
    service boundary rather than reaching the database layer at all.
    """
    if not isinstance(outcome, dict):
        raise ValueError("provider evaluate() must return a dict")  # noqa: TRY004
    if "score" not in outcome or "max_score" not in outcome:
        raise ValueError("provider evaluate() result must contain 'score' and 'max_score'")

    score = outcome["score"]
    max_score = outcome["max_score"]

    if type(score) is not int or type(max_score) is not int:
        raise ValueError("provider evaluate() 'score' and 'max_score' must be exact integers")
    if score < 0 or max_score < 0:
        raise ValueError("provider evaluate() 'score' and 'max_score' must be non-negative")
    if score > max_score:
        raise ValueError("provider evaluate() 'score' cannot exceed 'max_score'")

    return score, max_score


def start_attempt(learner, activity_version):
    """Start a new Attempt against the exact given ActivityVersion.

    Re-checks published state against the persisted row rather than trusting
    a possibly-stale in-memory activity_version object (ADR-009 service
    boundary; Task 05/06 lesson on protecting the boundary, not just the
    caller's discipline).
    """
    is_published = ActivityVersion.objects.filter(
        pk=activity_version.pk, published_at__isnull=False
    ).exists()
    if not is_published:
        raise ValueError("cannot start an Attempt against an unpublished ActivityVersion")

    return Attempt.objects.create(learner=learner, activity_version=activity_version)


def get_attempt_by_public_id(public_id):
    """Resolve an Attempt by its stable public_id, or None (T15-D27
    corrective HTTP flow): a plain read-only lookup with no ownership/
    authorization decision baked in -- callers (e.g. the Node HTTP
    adapter) decide what to do with the result, including whether the
    requesting identity may act on it. Returning None rather than
    raising Attempt.DoesNotExist keeps callers like linuxlab.sync.views
    from needing to import the Attempt model just to catch its
    auto-generated exception class.
    """
    return (
        Attempt.objects.select_related("learner", "installed_activity_version")
        .filter(public_id=public_id)
        .first()
    )


def claim_activity_completion(learner, activity_version):
    """Record an explicit, self-attested completion claim for one exact
    published ActivityVersion (Task 08, ADR-006/ADR-012 guest-first flow).

    This is NOT a graded evaluation: there is no original submission to
    re-run through the activity's provider (browser-local guest state is
    untrusted and was never a server-side record before claim), so the
    created Result is honest about its own provenance
    (details.source == "guest_claim") rather than pretending to be
    server-graded output. Never touches submit_attempt()'s provider-based
    finalization path or any pre-existing Attempt/Result row.

    Idempotent: repeating the identical claim for the same learner/
    activity_version is a safe no-op (returns None), so replaying a claim
    request never creates duplicate history or a duplicate DomainEvent.
    Concurrency-safe: locks the Learner row so two concurrent identical
    claims for the same learner serialize instead of racing past the
    idempotency check.
    """
    with transaction.atomic():
        locked_learner = Learner.objects.select_for_update().get(pk=learner.pk)

        already_claimed = Result.objects.filter(
            attempt__learner=locked_learner,
            attempt__activity_version=activity_version,
            details__source=_GUEST_CLAIM_SOURCE,
        ).exists()
        if already_claimed:
            return None

        attempt = start_attempt(locked_learner, activity_version)

        submission = {"source": _GUEST_CLAIM_SOURCE}
        submitted_at = timezone.now()
        updated = Attempt.objects.filter(pk=attempt.pk, submitted_at__isnull=True).update(
            submission=submission,
            submitted_at=submitted_at,
        )
        if updated != 1:
            raise ValueError("Attempt has already been submitted")

        attempt.submission = submission
        attempt.submitted_at = submitted_at

        details = {"source": _GUEST_CLAIM_SOURCE, "claimed_at": submitted_at.isoformat()}
        result = Result.objects.create(
            attempt=attempt,
            score=1,
            max_score=1,
            details=details,
        )

        refresh_progress(
            locked_learner.pk, activity_version.definition.public_id, activity_version.definition_id
        )

        domain_event = publish_event(
            "activity.completed",
            {
                "attempt_public_id": str(attempt.public_id),
                "learner_public_id": str(locked_learner.public_id),
                "activity_definition_public_id": str(activity_version.definition.public_id),
                "activity_version": activity_version.version,
                "score": 1,
                "max_score": 1,
                "source": _GUEST_CLAIM_SOURCE,
            },
            aggregate_id=attempt.public_id,
        )
        enqueue_sync_event(
            domain_event,
            node_id=settings.LINUXLAB_NODE_ID,
            sync_enabled=settings.LINUXLAB_SYNC_ENABLED,
        )

        return result


def _parse_claim_item(item):
    if not isinstance(item, dict):
        raise ValueError("each claim must be an object")  # noqa: TRY004

    actual_keys = set(item.keys())
    extra = actual_keys - _CLAIM_ITEM_KEYS
    if extra:
        raise ValueError(f"claim contains unexpected fields: {sorted(extra)}")
    missing = _CLAIM_ITEM_KEYS - actual_keys
    if missing:
        raise ValueError(f"claim is missing fields: {sorted(missing)}")

    raw_public_id = item["activity_public_id"]
    try:
        activity_public_id = uuid.UUID(str(raw_public_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("activity_public_id must be a valid UUID") from exc

    version = item["activity_version"]
    # type(x) is int, not isinstance: isinstance(True, int) is True in
    # Python, which would let activity_version=True silently match version 1.
    if type(version) is not int or version < 1:
        raise ValueError("activity_version must be an integer >= 1")

    return activity_public_id, version


def _resolve_and_validate_claims(claims):
    """Validate every claimed activity reference resolves to a published
    ActivityVersion BEFORE any claim is recorded (fail-closed batch
    validation): browser-local guest state is untrusted input, so nothing
    is claimed unless the whole batch checks out (Task 08).
    """
    if not isinstance(claims, list) or not claims:
        raise ValueError("claims must be a non-empty list")

    resolved = []
    for item in claims:
        activity_public_id, version = _parse_claim_item(item)
        try:
            activity_version = get_activity_version(activity_public_id, version)
        except ActivityVersion.DoesNotExist as exc:
            raise ValueError(
                f"no published ActivityVersion for {activity_public_id} v{version}"
            ) from exc
        resolved.append(activity_version)

    return resolved


def claim_guest_progress(account, claims):
    """Explicitly claim a caller-selected list of guest-local completions
    into the authenticated account's durable Learner history (Task 08,
    ADR-006/ADR-012).

    Every item is validated against a real published ActivityVersion before
    anything is recorded (fail-closed: one invalid item rejects the whole
    batch, and nothing is persisted). Only resolves/creates the Learner via
    the existing ensure_learner() -- no new identity concept. Each valid
    item is recorded through claim_activity_completion(), which is itself
    idempotent, so repeating an identical claims list is a safe no-op.
    """
    activity_versions = _resolve_and_validate_claims(claims)

    with transaction.atomic():
        learner = ensure_learner(account)

        results = []
        for activity_version in activity_versions:
            result = claim_activity_completion(learner, activity_version)
            if result is not None:
                results.append(result)

        return learner, results


def _lock_attempt_for_submission(attempt):
    """Re-read/lock the Attempt row rather than trusting the caller's
    in-memory object (Task 05/06 stale-object lesson): two concurrent
    submissions of the SAME attempt serialize on this row lock, and the
    loser observes submitted_at already set and is rejected.

    Deliberately does NOT select_related() across activity_version or
    installed_activity_version: both are nullable (T15-D27), so Django
    would emit a LEFT OUTER JOIN, and PostgreSQL rejects `SELECT ... FOR
    UPDATE` on the nullable side of an outer join. Whichever execution
    source is actually set is instead read as a normal (unlocked) follow-up
    access -- correct, since only the Attempt row itself needs locking.
    """
    locked_attempt = (
        Attempt.objects.select_for_update().select_related("learner").get(pk=attempt.pk)
    )
    if locked_attempt.submitted_at is not None:
        raise ValueError("Attempt has already been submitted")
    return locked_attempt


def _finalize_submission(
    locked_attempt, submission, score, max_score, details, *, activity_definition_public_id, activity_definition_id
):
    """Shared finalization for both execution sources (T15-D27 S9):
    transaction boundary, Result persistence, Progress update, and
    activity.completed domain event + Outbox enqueue. Callers have already
    locked the Attempt row and evaluated the submission through the
    correct provider/resolver for their execution source; only the
    finalization mechanics are identical either way.
    """
    # Deliberate, narrow bypass of Attempt.save()'s guard: finalization is
    # the ONE service-owned write path, so it persists the first submission
    # transition through a conditional QuerySet.update() rather than an
    # ordinary save() (which now rejects this transition unconditionally,
    # on purpose). The row is already select_for_update locked by the
    # caller, so this update is not itself a race; the
    # submitted_at__isnull=True filter plus the row-count check are a
    # defensive assertion of that invariant, not the actual guard.
    submitted_at = timezone.now()
    updated = Attempt.objects.filter(pk=locked_attempt.pk, submitted_at__isnull=True).update(
        submission=submission,
        submitted_at=submitted_at,
    )
    if updated != 1:
        raise ValueError("Attempt has already been submitted")

    locked_attempt.submission = submission
    locked_attempt.submitted_at = submitted_at

    result = Result.objects.create(
        attempt=locked_attempt,
        score=score,
        max_score=max_score,
        details=details,
    )

    refresh_progress(locked_attempt.learner_id, activity_definition_public_id, activity_definition_id)

    if locked_attempt.activity_version_id is not None:
        execution_source_version = locked_attempt.activity_version.version
    else:
        execution_source_version = locked_attempt.installed_activity_version.version

    domain_event = publish_event(
        "activity.completed",
        {
            "attempt_public_id": str(locked_attempt.public_id),
            "learner_public_id": str(locked_attempt.learner.public_id),
            "activity_definition_public_id": str(activity_definition_public_id),
            "activity_version": execution_source_version,
            "score": score,
            "max_score": max_score,
        },
        aggregate_id=locked_attempt.public_id,
    )
    enqueue_sync_event(
        domain_event,
        node_id=settings.LINUXLAB_NODE_ID,
        sync_enabled=settings.LINUXLAB_SYNC_ENABLED,
    )

    return result


def submit_attempt(attempt, submission, registry):
    """Evaluate a submission through the provider registry and record the
    single authoritative Result for this Attempt (Central execution
    source: Attempt.activity_version).
    """
    with transaction.atomic():
        locked_attempt = _lock_attempt_for_submission(attempt)
        if locked_attempt.activity_version_id is None:
            raise ValueError("Attempt has no Central activity_version execution source")

        activity_version = locked_attempt.activity_version
        provider = registry.get(activity_version.definition.type_key)
        outcome = provider.evaluate(activity_version.config, submission)
        score, max_score = _validate_provider_outcome(outcome)

        return _finalize_submission(
            locked_attempt,
            submission,
            score,
            max_score,
            outcome,
            activity_definition_public_id=activity_version.definition.public_id,
            activity_definition_id=activity_version.definition_id,
        )


def start_installed_attempt(learner, context, activity_public_id, version):
    """Start a new Attempt against an exact installed activity pinned by
    `context` (T15-D27 S6): the Node execution source counterpart to
    start_attempt().

    Requires the package underlying `context` to be currently ACTIVE: new
    executions may only begin against the active package (T15-D27 S7) --
    an existing Attempt, once started, stays pinned to its package even
    after a newer package activates or this one is archived, but nothing
    NEW may start against a non-active package.

    T15-D27 corrective (Important finding): the ACTIVE check and the
    Attempt INSERT are one atomic, LOCKED serialization boundary
    (sync.services.lock_active_installed_activity_for_execution()) --
    never a plain unlocked read followed by a separate insert, which
    would let a concurrent activate_package()/rollback_package() archive
    this exact package in the window between the two. Context provenance
    (proof `context` was genuinely minted by open_runtime_context(), not
    hand-constructed) is verified first and separately -- it is a pure
    check with no database access -- but the package/activity identity
    and ACTIVE state themselves are always re-verified fresh, under lock,
    never trusted from the context's already-read fields.
    """
    from linuxlab.runtime.context import verify_context_provenance
    from linuxlab.runtime.exceptions import RuntimeArtifactNotFound
    from linuxlab.sync.services import (
        InstalledActivityNotFoundError,
        PackageActivationError,
        lock_active_installed_activity_for_execution,
    )

    verify_context_provenance(context)

    with transaction.atomic():
        try:
            with lock_active_installed_activity_for_execution(
                package_pk=context.package_pk,
                package_type=context.package_type,
                source_public_id=context.source_public_id,
                package_version=context.package_version,
                payload_sha256=context.payload_sha256,
                activity_public_id=activity_public_id,
                version=version,
            ) as installed_activity:
                return Attempt.objects.create(
                    learner=learner, installed_activity_version=installed_activity
                )
        except InstalledActivityNotFoundError as exc:
            raise RuntimeArtifactNotFound(str(exc)) from exc
        except PackageActivationError as exc:
            raise ValueError(str(exc)) from exc


def submit_installed_attempt(attempt, submission, activity_registry):
    """Evaluate a submission through the provider registry and record the
    single authoritative Result for this Attempt (Node execution source:
    Attempt.installed_activity_version, T15-D27 S6/S8).

    Grading is pinned to whichever InstalledPackage this Attempt actually
    started against (open_pinned_context()), never re-resolved to "the
    current active package" -- an Attempt started against package A stays
    pinned to A even after package B later activates (T15-D27 S7).
    """
    with transaction.atomic():
        locked_attempt = _lock_attempt_for_submission(attempt)
        if locked_attempt.installed_activity_version_id is None:
            raise ValueError("Attempt has no installed_activity_version execution source")

        installed_activity = locked_attempt.installed_activity_version
        context = open_pinned_context(installed_activity.package)

        provider = activity_registry.get(installed_activity.type_key)
        outcome = provider.evaluate(installed_activity.config, submission, context=context)
        score, max_score = _validate_provider_outcome(outcome)

        return _finalize_submission(
            locked_attempt,
            submission,
            score,
            max_score,
            outcome,
            activity_definition_public_id=installed_activity.activity_public_id,
            activity_definition_id=None,
        )


def submit(attempt, submission, activity_registry):
    """Source-dispatch entry point (T15-D27 S9): inspect the Attempt's own
    execution source and route to the matching handler. Never
    profile-based -- the decision is made from the Attempt row itself, so
    it is correct regardless of which deployment profile happens to be
    running the request.
    """
    if attempt.installed_activity_version_id is not None:
        return submit_installed_attempt(attempt, submission, activity_registry)
    return submit_attempt(attempt, submission, activity_registry)
