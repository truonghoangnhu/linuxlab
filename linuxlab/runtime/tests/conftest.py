"""Shared Task 13 runtime test fixtures: a real Question -> Assessment
ActivityVersion -> Lesson -> PublicationSnapshot -> signed `.llpkg` ->
staged+activated InstalledPackage chain, built through the accepted
Task 09/10/11/12 production services (import_package()/activate_package()),
never through public ORM fabrication."""

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import override_settings

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.assessment.models import Question
from linuxlab.assessment.providers import SingleChoiceProvider
from linuxlab.assessment.registry import QuestionRegistry
from linuxlab.assessment.services import create_question_version, publish_question_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.package import build_package
from linuxlab.publication.snapshots import publish_learning_path
from linuxlab.quiz.provider import AssessmentActivityProvider
from linuxlab.sync.services import activate_package, import_package

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
    QuestionVersion dependency). Mirrors linuxlab/sync/tests/conftest.py's
    fixture of the same name, duplicated per this repository's convention
    of small per-app test bootstrap helpers."""
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


def republish(world):
    """Publish a NEW PublicationSnapshot version from the same live
    LearningPathPage/registry -- a distinct package_version of the same
    (package_type, source_public_id)."""
    live_path = LearningPathPage.objects.get(public_id=world["snapshot"].source_public_id)
    return publish_learning_path(live_path, world["activity_registry"])


def stage(world, private_key, public_key):
    """Build+sign a real `.llpkg` from world["snapshot"] and stage it as a
    Task 12 InstalledPackage through the real import_package() service."""
    package_bytes = build_package(world["snapshot"], world["activity_registry"], private_key)
    return import_package(package_bytes, world["activity_registry"], public_key)


@pytest.fixture
def installed_active_world():
    """A real Task 09-12 chain, staged AND activated: an ACTIVE
    InstalledPackage plus its InstalledPublication/InstalledActivityVersion/
    InstalledQuestionVersion/InstalledQuestionGrading rows, ready for
    Task 13 runtime tests. Requires @pytest.mark.django_db(transaction=True)
    (publish_learning_path() sets its own REPEATABLE READ isolation level)
    and @override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True) (import_package()
    is Node-profile gated) on the calling test.
    """
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    world = build_quiz_world()

    with override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True):
        package = stage(world, private_key, public_key)
        activate_package(package)

    world["package"] = package
    world["private_key"] = private_key
    world["public_key"] = public_key
    return world
