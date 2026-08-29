import uuid

import pytest

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.assessment.models import Question
from linuxlab.assessment.providers import SingleChoiceProvider
from linuxlab.assessment.registry import QuestionRegistry
from linuxlab.assessment.services import create_question_version, publish_question_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.validators import validate_learning_path
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


class FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": 1, "max_score": 1}


@pytest.fixture
def registry():
    reg = ActivityRegistry()
    reg.register(FakeProvider())
    return reg


@pytest.fixture
def definition(db):
    return ActivityDefinition.objects.create(name="Fake activity", type_key="fake")


@pytest.fixture
def published_version(definition, registry):
    version = create_activity_version(definition, config={})
    return publish_activity_version(version, registry)


def _root():
    return get_or_create_root_page()


def _make_tree(activity_block_value=None):
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    if activity_block_value is not None:
        lesson.body = [("activity", activity_block_value)]
    module.add_child(instance=lesson)
    return path


@pytest.mark.django_db(transaction=True)
def test_learning_path_with_no_activity_references_is_valid(registry):
    path = _make_tree()

    validate_learning_path(path, registry)  # must not raise


@pytest.mark.django_db(transaction=True)
def test_valid_published_activity_reference_passes(published_version, registry):
    path = _make_tree(
        {
            "activity_public_id": str(published_version.definition.public_id),
            "activity_version": published_version.version,
        }
    )

    validate_learning_path(path, registry)  # must not raise


@pytest.mark.django_db(transaction=True)
def test_missing_activity_definition_is_rejected(registry):
    path = _make_tree({"activity_public_id": str(uuid.uuid4()), "activity_version": 1})

    with pytest.raises(ValueError):
        validate_learning_path(path, registry)


@pytest.mark.django_db(transaction=True)
def test_unpublished_activity_version_is_rejected(definition, registry):
    unpublished = create_activity_version(definition, config={})
    path = _make_tree(
        {
            "activity_public_id": str(definition.public_id),
            "activity_version": unpublished.version,
        }
    )

    with pytest.raises(ValueError):
        validate_learning_path(path, registry)


@pytest.mark.django_db(transaction=True)
def test_wrong_version_number_is_rejected(published_version, registry):
    path = _make_tree(
        {
            "activity_public_id": str(published_version.definition.public_id),
            "activity_version": published_version.version + 1,
        }
    )

    with pytest.raises(ValueError):
        validate_learning_path(path, registry)


@pytest.mark.django_db(transaction=True)
def test_activity_reference_to_a_type_key_with_no_registered_provider_is_rejected(
    definition, registry
):
    # published via a registry that DOES have "fake" registered, but the
    # LearningPath is then validated against a registry that does not.
    version = create_activity_version(definition, config={})
    publish_activity_version(version, registry)

    empty_registry = ActivityRegistry()
    path = _make_tree(
        {"activity_public_id": str(definition.public_id), "activity_version": version.version}
    )

    with pytest.raises(ValueError):
        validate_learning_path(path, empty_registry)


# --- Task 09 corrective (Correction D, section 25): publication must not --
# --- trust an already-"published" ActivityVersion whose own dependencies --
# --- were never really checked. ---------------------------------------


class _LenientAssessmentProvider:
    """Stands in for a caller who publishes an assessment ActivityVersion
    through publish_activity_version() using a registry that does NOT wire
    up the real AssessmentActivityProvider -- an ordinary, currently
    possible path, since ActivityRegistry has no global state and any
    caller may supply any provider for a given type_key. The resulting
    ActivityVersion is published_at-not-null but was never actually checked
    against its configured QuestionVersion dependencies.
    """

    type_key = "assessment"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        raise NotImplementedError


def _malformed_published_assessment_activity_version(question_public_id, question_version):
    lenient_registry = ActivityRegistry()
    lenient_registry.register(_LenientAssessmentProvider())
    definition = ActivityDefinition.objects.create(name="Shell quiz", type_key="assessment")
    version = create_activity_version(
        definition,
        config={
            "questions": [
                {
                    "question_public_id": str(question_public_id),
                    "question_version": question_version,
                }
            ]
        },
    )
    return publish_activity_version(version, lenient_registry)


@pytest.mark.django_db(transaction=True)
def test_published_activity_version_referencing_a_missing_question_version_is_rejected():
    real_registry = ActivityRegistry()
    question_registry = QuestionRegistry()
    question_registry.register(SingleChoiceProvider())
    real_registry.register(AssessmentActivityProvider(question_registry))

    malformed = _malformed_published_assessment_activity_version(uuid.uuid4(), 1)
    path = _make_tree(
        {
            "activity_public_id": str(malformed.definition.public_id),
            "activity_version": malformed.version,
        }
    )

    with pytest.raises(ValueError):
        validate_learning_path(path, real_registry)


@pytest.mark.django_db(transaction=True)
def test_published_activity_version_referencing_an_unpublished_question_version_is_rejected():
    real_registry = ActivityRegistry()
    question_registry = QuestionRegistry()
    question_registry.register(SingleChoiceProvider())
    real_registry.register(AssessmentActivityProvider(question_registry))

    question = Question.objects.create(name="Shell basics Q1")
    unpublished_question_version = create_question_version(
        question,
        type_key="single_choice",
        content=CONTENT,
        answer_key=ANSWER_KEY,
        scoring=SCORING,
    )

    malformed = _malformed_published_assessment_activity_version(
        question.public_id, unpublished_question_version.version
    )
    path = _make_tree(
        {
            "activity_public_id": str(malformed.definition.public_id),
            "activity_version": malformed.version,
        }
    )

    with pytest.raises(ValueError):
        validate_learning_path(path, real_registry)


@pytest.mark.django_db(transaction=True)
def test_published_activity_version_with_published_question_version_passes():
    real_registry = ActivityRegistry()
    question_registry = QuestionRegistry()
    question_registry.register(SingleChoiceProvider())
    real_registry.register(AssessmentActivityProvider(question_registry))

    question = Question.objects.create(name="Shell basics Q1")
    question_version = create_question_version(
        question,
        type_key="single_choice",
        content=CONTENT,
        answer_key=ANSWER_KEY,
        scoring=SCORING,
    )
    publish_question_version(question_version, question_registry)

    malformed = _malformed_published_assessment_activity_version(
        question.public_id, question_version.version
    )
    path = _make_tree(
        {
            "activity_public_id": str(malformed.definition.public_id),
            "activity_version": malformed.version,
        }
    )

    validate_learning_path(path, real_registry)  # must not raise
