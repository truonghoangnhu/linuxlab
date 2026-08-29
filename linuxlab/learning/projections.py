from django.db import IntegrityError, transaction

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


def _meets_completion_threshold(result):
    config = result.attempt.activity_version.config or {}
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


def _lock_or_create_progress(learner_id, activity_definition_id):
    progress = (
        Progress.objects.select_for_update()
        .filter(learner_id=learner_id, activity_definition_id=activity_definition_id)
        .first()
    )
    if progress is not None:
        return progress

    try:
        with transaction.atomic():
            return Progress.objects.create(
                learner_id=learner_id,
                activity_definition_id=activity_definition_id,
                completed=False,
                best_score=0,
            )
    except IntegrityError:
        # A concurrent transaction created it first; this re-select blocks
        # until that transaction commits, then returns the locked row.
        return Progress.objects.select_for_update().get(
            learner_id=learner_id, activity_definition_id=activity_definition_id
        )


def refresh_progress(learner_id, activity_definition_id):
    """Recompute Progress for one (learner, activity_definition) pair from
    every currently-committed Result.

    The Progress row is locked/created BEFORE the Result query runs, so any
    concurrent refresh for the same pair is serialized on this row and always
    recomputes from a fully-committed Result set (Task 07 concurrency
    requirement) rather than racing a stale read against a stale write.
    """
    with transaction.atomic():
        progress = _lock_or_create_progress(learner_id, activity_definition_id)

        results = list(
            Result.objects.filter(
                attempt__learner_id=learner_id,
                attempt__activity_version__definition_id=activity_definition_id,
            ).select_related("attempt__activity_version")
        )

        if not results:
            progress.delete()
            return None

        completed, best_score = compute_progress_values(results)
        progress.completed = completed
        progress.best_score = best_score
        progress.save(update_fields=["completed", "best_score"])
        return progress


def _rebuild_pairs(learner_id, activity_definition_id):
    """Every (learner_id, activity_definition_id) pair a rebuild must visit
    within the given scope: the union of pairs backed by Result history AND
    pairs that currently have a Progress row.

    Including existing-Progress pairs (not just Result-backed ones) is what
    lets a rebuild PRUNE a stale/orphaned Progress row that has no
    authoritative history left -- refresh_progress() already deletes such a
    row once it's asked to look at it; the defect being fixed here was that
    an unscoped/filtered rebuild previously never asked.
    """
    result_pairs = Result.objects.values_list(
        "attempt__learner_id", "attempt__activity_version__definition_id"
    ).distinct()
    progress_pairs = Progress.objects.values_list("learner_id", "activity_definition_id").distinct()

    if learner_id is not None:
        result_pairs = result_pairs.filter(attempt__learner_id=learner_id)
        progress_pairs = progress_pairs.filter(learner_id=learner_id)
    if activity_definition_id is not None:
        result_pairs = result_pairs.filter(attempt__activity_version__definition_id=activity_definition_id)
        progress_pairs = progress_pairs.filter(activity_definition_id=activity_definition_id)

    return set(result_pairs) | set(progress_pairs)


def rebuild_progress(*, learner=None, activity_definition=None):
    """Rebuild Progress solely from authoritative Attempt/Result history,
    within the given scope.

    - both None: full rebuild -- every pair with Result history and every
      pair with an existing Progress row, anywhere.
    - learner only / activity_definition only: every matching pair for that
      learner / that definition, respectively.
    - both given: exactly that one pair.

    A pair that currently has NO Result history is still visited (via the
    existing-Progress half of the scope) so refresh_progress() can prune it:
    old Progress values are never used as rebuild input, only as a signal
    that the pair needs to be checked against current authoritative history.
    """
    learner_id = learner.pk if learner is not None else None
    activity_definition_id = activity_definition.pk if activity_definition is not None else None

    pairs = _rebuild_pairs(learner_id, activity_definition_id)

    for pair_learner_id, pair_activity_definition_id in pairs:
        refresh_progress(pair_learner_id, pair_activity_definition_id)
