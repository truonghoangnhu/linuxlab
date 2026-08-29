import json

import pytest

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.assessment.models import Question
from linuxlab.assessment.providers import SingleChoiceProvider
from linuxlab.assessment.registry import QuestionRegistry
from linuxlab.assessment.services import create_question_version, publish_question_version
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Progress, Result
from linuxlab.learning.projections import rebuild_progress
from linuxlab.learning.services import start_attempt, submit_attempt
from linuxlab.quiz.provider import AssessmentActivityProvider

CONTENT = {
    "prompt": "Which command lists files?",
    "options": [
        {"id": "a", "text": "ls"},
        {"id": "b", "text": "rm"},
    ],
}
ANSWER_KEY = {"correct_option_id": "a"}
SCORING = {"max_score": 1}


@pytest.fixture
def question_registry():
    reg = QuestionRegistry()
    reg.register(SingleChoiceProvider())
    return reg


@pytest.fixture
def activity_registry(question_registry):
    reg = ActivityRegistry()
    reg.register(AssessmentActivityProvider(question_registry))
    return reg


@pytest.fixture
def published_question(db, question_registry):
    question = Question.objects.create(name="Shell basics Q1")
    version = create_question_version(
        question,
        type_key="single_choice",
        content=CONTENT,
        answer_key=ANSWER_KEY,
        scoring=SCORING,
    )
    return publish_question_version(version, question_registry)


@pytest.fixture
def published_assessment_activity(db, published_question, activity_registry):
    definition = ActivityDefinition.objects.create(name="Shell quiz", type_key="assessment")
    version = create_activity_version(
        definition,
        config={
            "questions": [
                {
                    "question_public_id": str(published_question.question.public_id),
                    "question_version": published_question.version,
                }
            ]
        },
    )
    return publish_activity_version(version, activity_registry)


@pytest.fixture
def learner(db):
    return Learner.objects.create(account=None)


@pytest.mark.django_db
def test_real_assessment_activity_through_registry_produces_learner_safe_result_and_progress(
    learner, published_assessment_activity, activity_registry
):
    attempt = start_attempt(learner, published_assessment_activity)

    submission = {
        "responses": [
            {
                "question_public_id": str(published_assessment_activity.config["questions"][0]["question_public_id"]),
                "question_version": 1,
                "selected_option_id": "a",
            }
        ]
    }

    result = submit_attempt(attempt, submission, activity_registry)

    assert result.score == 1
    assert result.max_score == 1

    serialized = json.dumps(result.details)
    assert "answer_key" not in serialized
    assert "correct_option_id" not in serialized

    progress = Progress.objects.get(
        learner=learner, activity_definition=published_assessment_activity.definition
    )
    assert progress.completed is True
    assert progress.best_score == 1

    progress_serialized = json.dumps({"completed": progress.completed, "best_score": progress.best_score})
    assert "answer_key" not in progress_serialized
    assert "correct_option_id" not in progress_serialized

    # Delete + rebuild restores the same state (correctness oracle).
    Progress.objects.filter(pk=progress.pk).delete()
    rebuild_progress()

    rebuilt = Progress.objects.get(
        learner=learner, activity_definition=published_assessment_activity.definition
    )
    assert rebuilt.completed is True
    assert rebuilt.best_score == 1


@pytest.mark.django_db
def test_no_answer_key_material_in_result_records(learner, published_assessment_activity, activity_registry):
    attempt = start_attempt(learner, published_assessment_activity)
    question_public_id = str(published_assessment_activity.config["questions"][0]["question_public_id"])
    submission = {
        "responses": [
            {"question_public_id": question_public_id, "question_version": 1, "selected_option_id": "b"}
        ]
    }

    result = submit_attempt(attempt, submission, activity_registry)

    assert result.score == 0
    assert Result.objects.filter(attempt=attempt).exists()
    for stored in Result.objects.filter(attempt=attempt):
        dumped = json.dumps(stored.details)
        assert "answer_key" not in dumped
        assert "correct_option_id" not in dumped
