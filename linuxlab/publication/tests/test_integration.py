"""Required Task 09 integration scenario: real Task 04/05/06 objects, no
mocks for the core path (structured Lesson content, a real published
QuestionVersion, a real published Assessment ActivityVersion, an exact
activity reference, a LinuxLab publication, and proof that the frozen
snapshot is learner-safe and survives later draft edits)."""

import json

import pytest

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.assessment.models import Question
from linuxlab.assessment.providers import SingleChoiceProvider
from linuxlab.assessment.registry import QuestionRegistry
from linuxlab.assessment.services import create_question_version, publish_question_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.models import PublicationSnapshot
from linuxlab.publication.snapshots import publish_learning_path
from linuxlab.quiz.provider import AssessmentActivityProvider

from ._wagtail_bootstrap import get_or_create_root_page

CONTENT = {
    "prompt": "Which command lists files?",
    "options": [
        {"id": "a", "text": "ls"},
        {"id": "b", "text": "rm"},
    ],
}
ANSWER_KEY = {"correct_option_id": "a"}
SCORING = {"max_score": 1}


def _root():
    return get_or_create_root_page()


@pytest.mark.django_db
def test_real_content_publication_with_assessment_activity_end_to_end():
    # 1. structured Lesson content
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")

    # 2. real published QuestionVersion
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

    # 3. real published Assessment ActivityVersion
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

    # 4. exact activity reference placed into content
    lesson.body = [
        ("heading", "Try it yourself"),
        (
            "activity",
            {
                "activity_public_id": str(definition.public_id),
                "activity_version": published_activity.version,
            },
        ),
    ]
    module.add_child(instance=lesson)

    # 5. publish LinuxLab content snapshot
    snapshot = publish_learning_path(path, activity_registry)

    # 6/7. inspect immutable published snapshot, confirm exact ActivityVersion reference
    assert snapshot.version == 1
    blocks = snapshot.payload["learning_path"]["modules"][0]["lessons"][0]["blocks"]
    activity_block = next(b for b in blocks if b["type"] == "activity")
    assert activity_block["value"] == {
        "activity_public_id": str(definition.public_id),
        "activity_version": 1,
    }

    # 8. confirm learner-safe data contains no answer key anywhere in the snapshot
    serialized = json.dumps(snapshot.payload)
    assert "answer_key" not in serialized
    assert "correct_option_id" not in serialized
    assert "scoring" not in serialized
    assert "SERVER_SECRET" not in serialized

    original_payload = snapshot.payload
    original_hash = snapshot.content_hash

    # 9. edit mutable authoring content
    lesson.body = [("heading", "Changed after publication")]
    lesson.save_revision().publish()

    # 10. confirm old snapshot/hash unchanged
    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    assert reloaded.payload == original_payload
    assert reloaded.content_hash == original_hash


@pytest.mark.django_db
def test_required_failure_integration_unpublished_dependency_then_success():
    # Failure integration (Task 09 §48): reference an unpublished
    # ActivityVersion, confirm publication is rejected with no snapshot
    # persisted, then publish the dependency and retry successfully.
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")

    registry = ActivityRegistry()

    class FakeProvider:
        type_key = "fake"

        def validate_config(self, config):
            pass

        def evaluate(self, config, submission):
            return {"score": 1, "max_score": 1}

    registry.register(FakeProvider())
    definition = ActivityDefinition.objects.create(name="Fake activity", type_key="fake")
    unpublished = create_activity_version(definition, config={})

    lesson.body = [
        (
            "activity",
            {"activity_public_id": str(definition.public_id), "activity_version": unpublished.version},
        )
    ]
    module.add_child(instance=lesson)

    with pytest.raises(ValueError):
        publish_learning_path(path, registry)

    assert PublicationSnapshot.objects.filter(source_public_id=path.public_id).count() == 0

    publish_activity_version(unpublished, registry)

    snapshot = publish_learning_path(path, registry)
    assert snapshot.version == 1
