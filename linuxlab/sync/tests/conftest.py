"""Shared Task 12 test fixtures: a real Question -> Assessment
ActivityVersion -> Lesson -> PublicationSnapshot chain, built with the same
real Task 04/05/06/09 objects Task 11's own tests use (no mocks for the
core dependency-closure path)."""

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.assessment.models import Question
from linuxlab.assessment.providers import SingleChoiceProvider
from linuxlab.assessment.registry import QuestionRegistry
from linuxlab.assessment.services import create_question_version, publish_question_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.snapshots import publish_learning_path
from linuxlab.quiz.provider import AssessmentActivityProvider

from ._wagtail_bootstrap import get_or_create_root_page

CONTENT = {
    "prompt": "Which command lists files?",
    "options": [{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
}
ANSWER_KEY = {"correct_option_id": "a"}
SCORING = {"max_score": 1}


@pytest.fixture
def keypair():
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def build_quiz_world():
    """Publish a real LearningPath with one Lesson referencing one real
    published Assessment ActivityVersion (with one real published
    QuestionVersion dependency), and return everything a test might need,
    including the live LearningPathPage for republishing a newer version.
    """
    question_registry = QuestionRegistry()
    question_registry.register(SingleChoiceProvider())
    question = Question.objects.create(name="Shell basics Q1")
    question_version = create_question_version(
        question,
        type_key="single_choice",
        content=CONTENT,
        answer_key=ANSWER_KEY,
        scoring=SCORING,
    )
    published_question = publish_question_version(question_version, question_registry)

    activity_registry = ActivityRegistry()
    activity_registry.register(AssessmentActivityProvider(question_registry))
    definition = ActivityDefinition.objects.create(name="Shell quiz", type_key="assessment")
    activity_version = create_activity_version(
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
    published_activity = publish_activity_version(activity_version, activity_registry)

    root = get_or_create_root_page()
    path = LearningPathPage(title="Linux Basics")
    root.add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(definition.public_id),
                "activity_version": published_activity.version,
            },
        )
    ]
    module.add_child(instance=lesson)

    snapshot = publish_learning_path(path, activity_registry)

    return {
        "path": path,
        "snapshot": snapshot,
        "activity_registry": activity_registry,
        "definition": definition,
        "activity_version": published_activity,
        "question": question,
        "question_version": published_question,
    }


@pytest.fixture
def quiz_world():
    # No `db` fixture dependency: publish_learning_path() sets its own
    # REPEATABLE READ transaction isolation level, which requires a real
    # (transaction=True) test transaction, not pytest-django's default
    # nested-atomic `db` wrapping. Every test using this fixture must
    # carry @pytest.mark.django_db(transaction=True) itself.
    return build_quiz_world()


def republish(world):
    """Publish a NEW PublicationSnapshot version from the same live
    LearningPathPage/registry -- a distinct package_version of the same
    (package_type, source_public_id), for activation/rollback tests."""
    from linuxlab.content.models import LearningPathPage as LivePath

    live_path = LivePath.objects.get(public_id=world["snapshot"].source_public_id)
    return publish_learning_path(live_path, world["activity_registry"])
