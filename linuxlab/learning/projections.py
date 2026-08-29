from django.db import IntegrityError, transaction

from linuxlab.learning.models import Progress, Result

# Fallback used only when an ActivityVersion's config omits
# "completion_threshold" entirely. It is a default, not a hardcoded rule: any
# provider config may override it per version (Task 07 requirement).
DEFAULT_COMPLETION_THRESHOLD = 1.0


def _meets_completion_threshold(result):
    config = result.attempt.activity_version.config or {}
    threshold = config.get("completion_threshold", DEFAULT_COMPLETION_THRESHOLD)

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


def rebuild_progress(*, learner=None, activity_definition=None):
    """Rebuild Progress solely from authoritative Attempt/Result history.

    With no arguments, rebuilds every (learner, activity_definition) pair
    that has Result history -- this is what makes "delete all Progress rows,
    then rebuild" a correctness oracle (Task 07 load-bearing invariant).
    """
    if (learner is None) != (activity_definition is None):
        raise ValueError("learner and activity_definition must be provided together")

    if learner is not None:
        pairs = [(learner.pk, activity_definition.pk)]
    else:
        pairs = list(
            Result.objects.values_list(
                "attempt__learner_id", "attempt__activity_version__definition_id"
            )
            .distinct()
            .order_by()
        )

    for learner_id, activity_definition_id in pairs:
        refresh_progress(learner_id, activity_definition_id)
