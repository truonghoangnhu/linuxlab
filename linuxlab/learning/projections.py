from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.functions import Coalesce

from linuxlab.activities.models import ActivityDefinition
from linuxlab.learning.models import Progress, Result

# Fallback used only when an ActivityVersion's config omits
# "completion_threshold" entirely. It is a default, not a hardcoded rule: any
# provider config may override it per version (Task 07 requirement).
DEFAULT_COMPLETION_THRESHOLD = 1.0


def _resolve_completion_threshold(config):
    raw_threshold = config.get("completion_threshold", DEFAULT_COMPLETION_THRESHOLD)

    # type(x) in (int, float), not isinstance: isinstance(True, int) is True
    # in Python, so an unvalidated provider config could let a bool through
    # (e.g. completion_threshold=False == 0, silently making ANY score
    # "complete"). Not every ActivityProvider validates this key as strictly
    # as AssessmentActivityProvider does, so the generic layer that actually
    # applies it stays defensive and falls back to the default instead.
    if type(raw_threshold) not in (int, float) or not (0 <= raw_threshold <= 1):
        return DEFAULT_COMPLETION_THRESHOLD

    return raw_threshold


def _config_for_attempt(attempt):
    """T15-D27: an Attempt has exactly one execution source; read config
    from whichever one is actually set rather than assuming Central."""
    if attempt.activity_version_id is not None:
        return attempt.activity_version.config
    return attempt.installed_activity_version.config


def _meets_completion_threshold(result):
    config = _config_for_attempt(result.attempt) or {}
    threshold = _resolve_completion_threshold(config)

    if result.max_score <= 0:
        return threshold <= 0

    return (result.score / result.max_score) >= threshold


def compute_progress_values(results):
    """Pure projection rule: derive (completed, best_score) from a Result set.

    This is the single computation shared by the post-submit refresh and by
    rebuild_progress(), so there is exactly one place that defines what
    Progress means (Task 07 correctness-oracle requirement).
    """
    best_score = max(result.score for result in results)
    completed = any(_meets_completion_threshold(result) for result in results)
    return completed, best_score


def _lock_or_create_progress(learner_id, activity_definition_public_id, activity_definition_id):
    progress = (
        Progress.objects.select_for_update()
        .filter(learner_id=learner_id, activity_definition_public_id=activity_definition_public_id)
        .first()
    )
    if progress is not None:
        return progress

    try:
        with transaction.atomic():
            return Progress.objects.create(
                learner_id=learner_id,
                activity_definition_id=activity_definition_id,
                activity_definition_public_id=activity_definition_public_id,
                completed=False,
                best_score=0,
            )
    except IntegrityError:
        # A concurrent transaction created it first; this re-select blocks
        # until that transaction commits, then returns the locked row.
        return Progress.objects.select_for_update().get(
            learner_id=learner_id, activity_definition_public_id=activity_definition_public_id
        )


def refresh_progress(learner_id, activity_definition_public_id, activity_definition_id=None):
    """Recompute Progress for one (learner, activity_definition_public_id)
    pair from every currently-committed Result, across both Central and
    Node-installed execution sources (T15-D27).

    activity_definition_id is the local Central ActivityDefinition PK when
    the caller already knows it (always known from the Central submit/claim
    path; never known -- always None -- from the Node installed-execution
    path, T15-D27 S4.3). A previously-unset value is filled in once known,
    but never cleared: a Progress row that has already learned its local
    Central identity keeps it.

    The Progress row is locked/created BEFORE the Result query runs, so any
    concurrent refresh for the same pair is serialized on this row and always
    recomputes from a fully-committed Result set (Task 07 concurrency
    requirement) rather than racing a stale read against a stale write.
    """
    with transaction.atomic():
        progress = _lock_or_create_progress(
            learner_id, activity_definition_public_id, activity_definition_id
        )

        results = list(
            Result.objects.filter(attempt__learner_id=learner_id)
            .filter(
                Q(attempt__activity_version__definition__public_id=activity_definition_public_id)
                | Q(
                    attempt__installed_activity_version__activity_public_id=activity_definition_public_id
                )
            )
            .select_related("attempt__activity_version", "attempt__installed_activity_version")
        )

        if not results:
            progress.delete()
            return None

        completed, best_score = compute_progress_values(results)
        progress.completed = completed
        progress.best_score = best_score
        if activity_definition_id is not None and progress.activity_definition_id is None:
            progress.activity_definition_id = activity_definition_id
        progress.save(update_fields=["completed", "best_score", "activity_definition_id"])
        return progress


def _resolve_local_activity_definition_id(activity_definition_public_id):
    """Best-effort local Central identity for a stable definition UUID:
    populated when a matching local ActivityDefinition row exists (always
    true on Central for content it authored), left None otherwise (always
    true on Node for package-only content -- Node never authors and never
    receives ActivityDefinition rows, T15-D27 S4.3)."""
    return (
        ActivityDefinition.objects.filter(public_id=activity_definition_public_id)
        .values_list("pk", flat=True)
        .first()
    )


def _rebuild_pairs(learner_id, activity_definition_public_id):
    """Every (learner_id, activity_definition_public_id) pair a rebuild must
    visit within the given scope: the union of pairs backed by Result
    history (from either execution source) AND pairs that currently have a
    Progress row.

    Including existing-Progress pairs (not just Result-backed ones) is what
    lets a rebuild PRUNE a stale/orphaned Progress row that has no
    authoritative history left -- refresh_progress() already deletes such a
    row once it's asked to look at it; the defect being fixed here was that
    an unscoped/filtered rebuild previously never asked.
    """
    result_qs = Result.objects.annotate(
        _definition_public_id=Coalesce(
            "attempt__activity_version__definition__public_id",
            "attempt__installed_activity_version__activity_public_id",
        )
    )
    if learner_id is not None:
        result_qs = result_qs.filter(attempt__learner_id=learner_id)
    if activity_definition_public_id is not None:
        result_qs = result_qs.filter(_definition_public_id=activity_definition_public_id)
    result_pairs = result_qs.values_list("attempt__learner_id", "_definition_public_id").distinct()

    progress_qs = Progress.objects.all()
    if learner_id is not None:
        progress_qs = progress_qs.filter(learner_id=learner_id)
    if activity_definition_public_id is not None:
        progress_qs = progress_qs.filter(activity_definition_public_id=activity_definition_public_id)
    progress_pairs = progress_qs.values_list("learner_id", "activity_definition_public_id").distinct()

    return set(result_pairs) | set(progress_pairs)


def rebuild_progress(*, learner=None, activity_definition=None):
    """Rebuild Progress solely from authoritative Attempt/Result history,
    within the given scope.

    - both None: full rebuild -- every pair with Result history and every
      pair with an existing Progress row, anywhere.
    - learner only / activity_definition only: every matching pair for that
      learner / that definition, respectively.
    - both given: exactly that one pair.

    `activity_definition` (when given) is a Central ActivityDefinition
    instance, as before Task 15; it is resolved to its stable public_id,
    which is what Progress is actually keyed by (T15-D27).

    A pair that currently has NO Result history is still visited (via the
    existing-Progress half of the scope) so refresh_progress() can prune it:
    old Progress values are never used as rebuild input, only as a signal
    that the pair needs to be checked against current authoritative history.
    """
    learner_id = learner.pk if learner is not None else None
    activity_definition_public_id = (
        activity_definition.public_id if activity_definition is not None else None
    )

    pairs = _rebuild_pairs(learner_id, activity_definition_public_id)

    for pair_learner_id, pair_activity_definition_public_id in pairs:
        activity_definition_id = _resolve_local_activity_definition_id(
            pair_activity_definition_public_id
        )
        refresh_progress(pair_learner_id, pair_activity_definition_public_id, activity_definition_id)
