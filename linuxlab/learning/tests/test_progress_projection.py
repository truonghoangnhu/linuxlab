import pytest

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Progress
from linuxlab.learning.projections import rebuild_progress
from linuxlab.learning.services import start_attempt, submit_attempt


class ScoredProvider:
    """Test-only provider: evaluate() just returns whatever score/max_score
    the submission dict specifies, so tests can drive Result outcomes
    directly instead of encoding grading logic here."""

    type_key = "scored"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": submission["score"], "max_score": submission["max_score"]}


@pytest.fixture
def registry():
    reg = ActivityRegistry()
    reg.register(ScoredProvider())
    return reg


@pytest.fixture
def definition(db):
    return ActivityDefinition.objects.create(name="Scored activity", type_key="scored")


@pytest.fixture
def learner(db):
    return Learner.objects.create(account=None)


@pytest.fixture
def published_version(definition, registry):
    version = create_activity_version(definition, config={})
    return publish_activity_version(version, registry)


def _submit(learner, activity_version, registry, score, max_score):
    attempt = start_attempt(learner, activity_version)
    return submit_attempt(attempt, {"score": score, "max_score": max_score}, registry)


# --- derivation from Result history --------------------------------------


@pytest.mark.django_db
def test_no_results_means_no_progress_row(learner, definition):
    assert Progress.objects.filter(learner=learner, activity_definition=definition).exists() is False


@pytest.mark.django_db
def test_one_perfect_result_yields_completed_progress(learner, definition, published_version, registry):
    _submit(learner, published_version, registry, score=1, max_score=1)

    progress = Progress.objects.get(learner=learner, activity_definition=definition)
    assert progress.completed is True
    assert progress.best_score == 1


@pytest.mark.django_db
def test_one_partial_result_is_not_completed_by_default(learner, definition, published_version, registry):
    _submit(learner, published_version, registry, score=1, max_score=2)

    progress = Progress.objects.get(learner=learner, activity_definition=definition)
    assert progress.completed is False
    assert progress.best_score == 1


@pytest.mark.django_db
def test_two_results_take_deterministic_best_score_and_completion(
    learner, definition, published_version, registry
):
    _submit(learner, published_version, registry, score=0, max_score=2)
    second_attempt = start_attempt(learner, published_version)
    submit_attempt(second_attempt, {"score": 2, "max_score": 2}, registry)

    progress = Progress.objects.get(learner=learner, activity_definition=definition)
    assert progress.best_score == 2
    assert progress.completed is True


# --- completion threshold read from Activity config -----------------------


@pytest.mark.django_db
def test_completion_threshold_is_read_from_activity_version_config(learner, definition, registry):
    lenient_version = create_activity_version(definition, config={"completion_threshold": 0.5})
    publish_activity_version(lenient_version, registry)

    _submit(learner, lenient_version, registry, score=1, max_score=2)

    progress = Progress.objects.get(learner=learner, activity_definition=definition)
    assert progress.completed is True


@pytest.mark.django_db
def test_stricter_threshold_in_a_later_version_does_not_retroactively_apply(
    learner, definition, published_version, registry
):
    # published_version has no completion_threshold override -> default 1.0
    _submit(learner, published_version, registry, score=1, max_score=2)

    progress = Progress.objects.get(learner=learner, activity_definition=definition)
    assert progress.completed is False


# --- delete + rebuild is a correctness oracle (load-bearing) -------------


@pytest.mark.django_db
def test_rebuild_after_deleting_progress_restores_the_same_state(
    learner, definition, published_version, registry
):
    _submit(learner, published_version, registry, score=0, max_score=2)
    second_attempt = start_attempt(learner, published_version)
    submit_attempt(second_attempt, {"score": 2, "max_score": 2}, registry)

    expected = Progress.objects.get(learner=learner, activity_definition=definition)
    expected_completed, expected_best_score = expected.completed, expected.best_score

    Progress.objects.filter(learner=learner, activity_definition=definition).delete()
    assert Progress.objects.filter(learner=learner, activity_definition=definition).exists() is False

    rebuild_progress()

    rebuilt = Progress.objects.get(learner=learner, activity_definition=definition)
    assert rebuilt.completed == expected_completed
    assert rebuilt.best_score == expected_best_score

    # Rebuilding again must be idempotent: no duplicates, no drift.
    rebuild_progress()

    assert Progress.objects.filter(learner=learner, activity_definition=definition).count() == 1
    reloaded = Progress.objects.get(learner=learner, activity_definition=definition)
    assert reloaded.completed == expected_completed
    assert reloaded.best_score == expected_best_score


@pytest.mark.django_db
def test_rebuild_progress_for_a_single_pair_does_not_touch_others(
    learner, definition, published_version, registry
):
    other_learner = Learner.objects.create(account=None)
    _submit(learner, published_version, registry, score=1, max_score=1)
    _submit(other_learner, published_version, registry, score=0, max_score=1)

    Progress.objects.all().delete()

    rebuild_progress(learner=learner, activity_definition=definition)

    assert Progress.objects.filter(learner=learner, activity_definition=definition).exists() is True
    assert Progress.objects.filter(learner=other_learner, activity_definition=definition).exists() is False


@pytest.mark.django_db
def test_rebuild_progress_removes_stale_row_when_results_no_longer_exist(
    learner, definition, published_version, registry
):
    _submit(learner, published_version, registry, score=1, max_score=1)
    assert Progress.objects.filter(learner=learner, activity_definition=definition).exists() is True

    from linuxlab.learning.models import Result

    Result.objects.filter(attempt__learner=learner).delete()

    rebuild_progress(learner=learner, activity_definition=definition)

    assert Progress.objects.filter(learner=learner, activity_definition=definition).exists() is False
