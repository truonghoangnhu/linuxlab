from django.db import transaction
from django.utils import timezone

from linuxlab.activities.models import ActivityVersion
from linuxlab.core.events import publish_event
from linuxlab.learning.models import Attempt, Result
from linuxlab.learning.projections import refresh_progress


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


def submit_attempt(attempt, submission, registry):
    """Evaluate a submission through the provider registry and record the
    single authoritative Result for this Attempt.

    Attempt is re-read/locked from the database rather than trusting the
    caller's in-memory object (Task 05/06 stale-object lesson): two
    concurrent submissions of the SAME attempt serialize on this row lock,
    and the loser observes submitted_at already set and is rejected.
    """
    with transaction.atomic():
        locked_attempt = (
            Attempt.objects.select_for_update()
            .select_related("activity_version__definition", "learner")
            .get(pk=attempt.pk)
        )
        if locked_attempt.submitted_at is not None:
            raise ValueError("Attempt has already been submitted")

        activity_version = locked_attempt.activity_version
        provider = registry.get(activity_version.definition.type_key)
        outcome = provider.evaluate(activity_version.config, submission)
        score, max_score = _validate_provider_outcome(outcome)

        locked_attempt.submission = submission
        locked_attempt.submitted_at = timezone.now()
        locked_attempt.save(update_fields=["submission", "submitted_at"])

        result = Result.objects.create(
            attempt=locked_attempt,
            score=score,
            max_score=max_score,
            details=outcome,
        )

        refresh_progress(locked_attempt.learner_id, activity_version.definition_id)

        publish_event(
            "activity.completed",
            {
                "attempt_public_id": str(locked_attempt.public_id),
                "learner_public_id": str(locked_attempt.learner.public_id),
                "activity_definition_public_id": str(activity_version.definition.public_id),
                "activity_version": activity_version.version,
                "score": score,
                "max_score": max_score,
            },
            aggregate_id=locked_attempt.public_id,
        )

        return result
