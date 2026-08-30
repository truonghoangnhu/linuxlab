import json
import uuid

import pytest

from linuxlab.activities.contracts import ActivityDependencies
from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.assessment.models import Question
from linuxlab.assessment.providers import SingleChoiceProvider
from linuxlab.assessment.registry import QuestionRegistry
from linuxlab.assessment.services import create_question_version, publish_question_version
from linuxlab.quiz.provider import AssessmentActivityProvider, serialize_assessment_for_learner

FIRST_CONTENT = {
    "prompt": "Which command lists files?",
    "options": [
        {"id": "a", "text": "ls"},
        {"id": "b", "text": "rm"},
    ],
}
FIRST_ANSWER_KEY = {"correct_option_id": "a"}
FIRST_SCORING = {"max_score": 1}

SECOND_CONTENT = {
    "prompt": "Which command prints the working directory?",
    "options": [
        {"id": "x", "text": "pwd"},
        {"id": "y", "text": "cd"},
    ],
}
SECOND_ANSWER_KEY = {"correct_option_id": "x"}
SECOND_SCORING = {"max_score": 2}


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
    question = Question.objects.create(name="Q1")
    version = create_question_version(
        question,
        type_key="single_choice",
        content=FIRST_CONTENT,
        answer_key=FIRST_ANSWER_KEY,
        scoring=FIRST_SCORING,
    )
    return publish_question_version(version, question_registry)


@pytest.fixture
def second_published_question(db, question_registry):
    question = Question.objects.create(name="Q2")
    version = create_question_version(
        question,
        type_key="single_choice",
        content=SECOND_CONTENT,
        answer_key=SECOND_ANSWER_KEY,
        scoring=SECOND_SCORING,
    )
    return publish_question_version(version, question_registry)


def _config_for(*question_versions):
    return {
        "questions": [
            {
                "question_public_id": str(qv.question.public_id),
                "question_version": qv.version,
            }
            for qv in question_versions
        ]
    }


# --- validate_config: valid ------------------------------------------------


@pytest.mark.django_db
def test_valid_exact_published_question_ref_is_accepted(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)

    provider.validate_config(_config_for(published_question))


# --- collect_dependencies: Task 11 package dependency closure --------------


@pytest.mark.django_db
def test_collect_dependencies_returns_the_exact_question_version_refs(
    published_question, second_published_question, question_registry
):
    provider = AssessmentActivityProvider(question_registry)

    dependencies = provider.collect_dependencies(
        _config_for(published_question, second_published_question)
    )

    assert dependencies == ActivityDependencies(
        question_versions=(
            (published_question.question.public_id, published_question.version),
            (second_published_question.question.public_id, second_published_question.version),
        )
    )


def test_activity_dependencies_default_is_empty():
    assert ActivityDependencies() == ActivityDependencies(question_versions=())


# --- validate_config: invalid -----------------------------------------------


@pytest.mark.django_db
def test_wrong_question_uuid_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {"questions": [{"question_public_id": str(uuid.uuid4()), "question_version": 1}]}

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_wrong_question_version_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {
        "questions": [
            {
                "question_public_id": str(published_question.question.public_id),
                "question_version": 999,
            }
        ]
    }

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_unpublished_question_version_is_rejected(question_registry):
    provider = AssessmentActivityProvider(question_registry)
    question = Question.objects.create(name="Draft")
    unpublished = create_question_version(
        question,
        type_key="single_choice",
        content=FIRST_CONTENT,
        answer_key=FIRST_ANSWER_KEY,
        scoring=FIRST_SCORING,
    )
    config = _config_for(unpublished)

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_version_zero_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {
        "questions": [
            {
                "question_public_id": str(published_question.question.public_id),
                "question_version": 0,
            }
        ]
    }

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_empty_questions_list_is_rejected(question_registry):
    provider = AssessmentActivityProvider(question_registry)

    with pytest.raises(ValueError):
        provider.validate_config({"questions": []})


@pytest.mark.django_db
def test_no_latest_semantics_missing_version_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {
        "questions": [{"question_public_id": str(published_question.question.public_id)}]
    }

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_boolean_question_version_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {
        "questions": [
            {
                "question_public_id": str(published_question.question.public_id),
                "question_version": True,
            }
        ]
    }

    with pytest.raises(ValueError):
        provider.validate_config(config)


# --- exact allow-list schemas (mandatory security tests) ------------------


@pytest.mark.django_db
def test_top_level_answer_key_in_config_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {
        **_config_for(published_question),
        "answer_key": {"secret": "SERVER_SECRET"},
    }

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_unknown_top_level_config_field_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {**_config_for(published_question), "solutions": ["a"]}

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_per_reference_correct_option_id_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {
        "questions": [
            {
                "question_public_id": str(published_question.question.public_id),
                "question_version": published_question.version,
                "correct_option_id": "b",
            }
        ]
    }

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_per_reference_points_field_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {
        "questions": [
            {
                "question_public_id": str(published_question.question.public_id),
                "question_version": published_question.version,
                "points": 5,
            }
        ]
    }

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_duplicate_configured_question_refs_are_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = _config_for(published_question, published_question)

    with pytest.raises(ValueError):
        provider.validate_config(config)


# --- completion_threshold (Task 07 corrective integration) ----------------


@pytest.mark.django_db
def test_valid_completion_threshold_is_accepted(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {**_config_for(published_question), "completion_threshold": 0.5}

    provider.validate_config(config)


@pytest.mark.django_db
def test_integer_completion_threshold_is_accepted(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {**_config_for(published_question), "completion_threshold": 1}

    provider.validate_config(config)


@pytest.mark.django_db
def test_boolean_completion_threshold_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {**_config_for(published_question), "completion_threshold": True}

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_string_completion_threshold_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = {**_config_for(published_question), "completion_threshold": "0.5"}

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_out_of_range_completion_threshold_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)

    with pytest.raises(ValueError):
        provider.validate_config({**_config_for(published_question), "completion_threshold": 1.5})

    with pytest.raises(ValueError):
        provider.validate_config({**_config_for(published_question), "completion_threshold": -0.1})


@pytest.mark.django_db
def test_completion_threshold_does_not_reopen_arbitrary_config_fields(
    published_question, question_registry
):
    provider = AssessmentActivityProvider(question_registry)
    config = {
        **_config_for(published_question),
        "completion_threshold": 0.5,
        "answer_key": {"secret": "SERVER_SECRET"},
    }

    with pytest.raises(ValueError):
        provider.validate_config(config)


@pytest.mark.django_db
def test_completion_threshold_cannot_be_placed_on_a_question_reference(
    published_question, question_registry
):
    provider = AssessmentActivityProvider(question_registry)
    config = {
        "questions": [
            {
                "question_public_id": str(published_question.question.public_id),
                "question_version": published_question.version,
                "completion_threshold": 0.5,
            }
        ]
    }

    with pytest.raises(ValueError):
        provider.validate_config(config)


# --- evaluate --------------------------------------------------------------


@pytest.mark.django_db
def test_evaluate_grades_correct_and_incorrect_answers(
    published_question, second_published_question, question_registry
):
    provider = AssessmentActivityProvider(question_registry)
    config = _config_for(published_question, second_published_question)
    submission = {
        "responses": [
            {
                "question_public_id": str(published_question.question.public_id),
                "question_version": published_question.version,
                "selected_option_id": "a",  # correct
            },
            {
                "question_public_id": str(second_published_question.question.public_id),
                "question_version": second_published_question.version,
                "selected_option_id": "y",  # incorrect
            },
        ]
    }

    result = provider.evaluate(config, submission)

    assert result["score"] == 1  # 1 (correct, max_score 1) + 0 (incorrect, max_score 2)
    assert result["max_score"] == 3
    assert result["results"][0]["correct"] is True
    assert result["results"][0]["score"] == 1
    assert result["results"][1]["correct"] is False
    assert result["results"][1]["score"] == 0
    assert result["results"][1]["max_score"] == 2


@pytest.mark.django_db
def test_evaluate_result_ordering_matches_config_order(
    published_question, second_published_question, question_registry
):
    provider = AssessmentActivityProvider(question_registry)
    config = _config_for(published_question, second_published_question)
    submission = {"responses": []}

    result = provider.evaluate(config, submission)

    assert result["results"][0]["question_public_id"] == str(published_question.question.public_id)
    assert result["results"][1]["question_public_id"] == str(
        second_published_question.question.public_id
    )


@pytest.mark.django_db
def test_evaluate_missing_response_is_deterministically_incorrect(
    published_question, question_registry
):
    provider = AssessmentActivityProvider(question_registry)
    config = _config_for(published_question)

    result = provider.evaluate(config, {"responses": []})

    assert result["results"][0]["correct"] is False
    assert result["score"] == 0


@pytest.mark.django_db
def test_evaluate_does_not_leak_answer_key(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = _config_for(published_question)

    result = provider.evaluate(config, {"responses": []})
    serialized = json.dumps(result)

    assert "answer_key" not in serialized
    assert "correct_option_id" not in serialized


# --- wrong version / duplicate response tests (Task 06 corrective) --------


@pytest.mark.django_db
def test_response_claiming_wrong_version_does_not_grade_against_configured_version(
    published_question, question_registry
):
    # configured Q v1; response claims Q v2 -> must not match v1's response slot
    provider = AssessmentActivityProvider(question_registry)
    config = _config_for(published_question)
    submission = {
        "responses": [
            {
                "question_public_id": str(published_question.question.public_id),
                "question_version": published_question.version + 1,
                "selected_option_id": "a",
            }
        ]
    }

    result = provider.evaluate(config, submission)

    assert result["results"][0]["correct"] is False


@pytest.mark.django_db
def test_response_with_boolean_version_is_not_matched(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = _config_for(published_question)
    submission = {
        "responses": [
            {
                "question_public_id": str(published_question.question.public_id),
                "question_version": True,
                "selected_option_id": "a",
            }
        ]
    }

    result = provider.evaluate(config, submission)

    assert result["results"][0]["correct"] is False


@pytest.mark.django_db
def test_duplicate_exact_response_pair_is_rejected(published_question, question_registry):
    provider = AssessmentActivityProvider(question_registry)
    config = _config_for(published_question)
    duplicate_response = {
        "question_public_id": str(published_question.question.public_id),
        "question_version": published_question.version,
        "selected_option_id": "a",
    }
    submission = {"responses": [duplicate_response, dict(duplicate_response)]}

    with pytest.raises(ValueError):
        provider.evaluate(config, submission)


# --- no learning-state persistence -----------------------------------------


@pytest.mark.django_db
def test_evaluate_does_not_write_learning_state(published_question, question_registry):
    from linuxlab.core.models import DomainEvent

    provider = AssessmentActivityProvider(question_registry)
    config = _config_for(published_question)
    before = DomainEvent.objects.count()

    provider.evaluate(config, {"responses": []})

    assert DomainEvent.objects.count() == before


# --- Task 05 integration ---------------------------------------------------


@pytest.mark.django_db
def test_activity_publication_uses_assessment_provider_validation(
    published_question, activity_registry
):
    definition = ActivityDefinition.objects.create(name="Shell Quiz", type_key="assessment")
    activity_version = create_activity_version(
        definition, config=_config_for(published_question)
    )

    published = publish_activity_version(activity_version, activity_registry)

    assert published.is_published is True


@pytest.mark.django_db
def test_broken_question_ref_blocks_activity_version_publication(activity_registry):
    definition = ActivityDefinition.objects.create(name="Shell Quiz", type_key="assessment")
    broken_config = {
        "questions": [{"question_public_id": str(uuid.uuid4()), "question_version": 1}]
    }
    activity_version = create_activity_version(definition, config=broken_config)

    with pytest.raises(ValueError):
        publish_activity_version(activity_version, activity_registry)

    activity_version.refresh_from_db()
    assert activity_version.published_at is None


# --- serialize_assessment_for_learner ---------------------------------


@pytest.mark.django_db
def test_serialize_assessment_for_learner_preserves_order(
    published_question, second_published_question, activity_registry, question_registry
):
    definition = ActivityDefinition.objects.create(name="Shell Quiz", type_key="assessment")
    activity_version = create_activity_version(
        definition, config=_config_for(published_question, second_published_question)
    )
    published = publish_activity_version(activity_version, activity_registry)

    payload = serialize_assessment_for_learner(published, question_registry)

    assert payload["activity_public_id"] == str(definition.public_id)
    assert payload["activity_version"] == 1
    assert len(payload["questions"]) == 2
    assert payload["questions"][0]["question_public_id"] == str(
        published_question.question.public_id
    )
    assert payload["questions"][1]["question_public_id"] == str(
        second_published_question.question.public_id
    )


@pytest.mark.django_db
def test_serialize_assessment_for_learner_requires_published_activity_version(
    published_question, question_registry
):
    definition = ActivityDefinition.objects.create(name="Shell Quiz", type_key="assessment")
    activity_version = create_activity_version(definition, config=_config_for(published_question))

    with pytest.raises(ValueError):
        serialize_assessment_for_learner(activity_version, question_registry)


@pytest.mark.django_db
def test_serialize_assessment_for_learner_never_leaks_answer_key(
    published_question, activity_registry, question_registry
):
    definition = ActivityDefinition.objects.create(name="Shell Quiz", type_key="assessment")
    activity_version = create_activity_version(definition, config=_config_for(published_question))
    published = publish_activity_version(activity_version, activity_registry)

    payload = serialize_assessment_for_learner(published, question_registry)
    serialized = json.dumps(payload)

    assert "answer_key" not in serialized
    assert "correct_option_id" not in serialized
    assert "scoring" not in serialized


# --- assessment serializer type guard (Task 06 corrective) -----------------


@pytest.mark.django_db
def test_serialize_assessment_for_learner_rejects_non_assessment_activity(question_registry):
    from linuxlab.activities.contracts import ActivityProvider  # noqa: F401
    from linuxlab.activities.registry import ActivityRegistry

    class FakeTerminalProvider:
        type_key = "terminal"

        def validate_config(self, config):
            pass

        def evaluate(self, config, submission):
            return {}

    terminal_registry = ActivityRegistry()
    terminal_registry.register(FakeTerminalProvider())

    definition = ActivityDefinition.objects.create(name="Terminal Lab", type_key="terminal")
    # config happens to resemble a valid assessment config
    activity_version = create_activity_version(definition, config={"questions": []})
    published = publish_activity_version(activity_version, terminal_registry)

    with pytest.raises(ValueError):
        serialize_assessment_for_learner(published, question_registry)
