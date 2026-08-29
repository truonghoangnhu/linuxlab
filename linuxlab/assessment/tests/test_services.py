import json
import uuid

import pytest

from linuxlab.assessment.models import Question, QuestionVersion
from linuxlab.assessment.providers import SingleChoiceProvider
from linuxlab.assessment.registry import QuestionRegistry
from linuxlab.assessment.services import (
    create_question_version,
    get_question_version,
    publish_question_version,
    serialize_question_for_learner,
)

TYPE_KEY = "single_choice"
VALID_CONTENT = {
    "prompt": "Which command lists files?",
    "options": [
        {"id": "a", "text": "ls"},
        {"id": "b", "text": "rm"},
    ],
}
VALID_ANSWER_KEY = {"correct_option_id": "a"}
VALID_SCORING = {"max_score": 1}


def _create_version(question, **overrides):
    fields = {
        "type_key": TYPE_KEY,
        "content": VALID_CONTENT,
        "answer_key": VALID_ANSWER_KEY,
        "scoring": VALID_SCORING,
    }
    fields.update(overrides)
    return create_question_version(question, **fields)


@pytest.fixture
def registry():
    reg = QuestionRegistry()
    reg.register(SingleChoiceProvider())
    return reg


@pytest.fixture
def question(db):
    return Question.objects.create(name="Shell basics quiz item")


# --- version creation ---------------------------------------------------


@pytest.mark.django_db
def test_first_created_version_is_one(question):
    version = _create_version(question)

    assert version.version == 1
    assert version.type_key == TYPE_KEY


@pytest.mark.django_db
def test_next_created_version_is_two(question):
    _create_version(question)
    version = _create_version(question)

    assert version.version == 2


@pytest.mark.django_db
def test_third_created_version_is_three(question):
    _create_version(question)
    _create_version(question)
    version = _create_version(question)

    assert version.version == 3


@pytest.mark.django_db
def test_next_version_may_use_a_different_type_key(question):
    _create_version(question, type_key="single_choice")
    version = _create_version(question, type_key="short_answer")

    assert version.version == 2
    assert version.type_key == "short_answer"


# --- publication ----------------------------------------------------------


@pytest.mark.django_db
def test_valid_content_and_answer_key_publish(question, registry):
    version = _create_version(question)

    published = publish_question_version(version, registry)

    assert published.published_at is not None
    assert published.is_published is True


@pytest.mark.django_db
def test_invalid_content_does_not_publish(question, registry):
    bad_content = {"prompt": "", "options": VALID_CONTENT["options"]}
    version = _create_version(question, content=bad_content)

    with pytest.raises(ValueError):
        publish_question_version(version, registry)

    reloaded = QuestionVersion.objects.get(pk=version.pk)
    assert reloaded.published_at is None


@pytest.mark.django_db
def test_invalid_answer_key_does_not_publish(question, registry):
    bad_answer_key = {"correct_option_id": "does-not-exist"}
    version = _create_version(question, answer_key=bad_answer_key)

    with pytest.raises(ValueError):
        publish_question_version(version, registry)

    reloaded = QuestionVersion.objects.get(pk=version.pk)
    assert reloaded.published_at is None


@pytest.mark.django_db
def test_invalid_scoring_does_not_publish(question, registry):
    version = _create_version(question, scoring={"max_score": 0})

    with pytest.raises(ValueError):
        publish_question_version(version, registry)

    reloaded = QuestionVersion.objects.get(pk=version.pk)
    assert reloaded.published_at is None


@pytest.mark.django_db
def test_already_published_version_cannot_be_republished(question, registry):
    version = _create_version(question)
    publish_question_version(version, registry)

    with pytest.raises(ValueError):
        publish_question_version(version, registry)


@pytest.mark.django_db
def test_changing_content_requires_a_new_version(question, registry):
    version = _create_version(question)
    publish_question_version(version, registry)

    next_version = _create_version(question)

    assert next_version.version == 2
    assert next_version.published_at is None


# --- published immutability ------------------------------------------------


@pytest.mark.django_db
def test_published_content_cannot_be_changed_through_normal_save(question, registry):
    version = _create_version(question)
    publish_question_version(version, registry)

    version.content = {**VALID_CONTENT, "prompt": "changed"}
    with pytest.raises(ValueError):
        version.save()

    reloaded = QuestionVersion.objects.get(pk=version.pk)
    assert reloaded.content == VALID_CONTENT


@pytest.mark.django_db
def test_published_answer_key_cannot_be_changed(question, registry):
    version = _create_version(question)
    publish_question_version(version, registry)

    version.answer_key = {"correct_option_id": "b"}
    with pytest.raises(ValueError):
        version.save()

    reloaded = QuestionVersion.objects.get(pk=version.pk)
    assert reloaded.answer_key == VALID_ANSWER_KEY


@pytest.mark.django_db
def test_published_scoring_cannot_be_changed(question, registry):
    version = _create_version(question)
    publish_question_version(version, registry)

    version.scoring = {"max_score": 99}
    with pytest.raises(ValueError):
        version.save()

    reloaded = QuestionVersion.objects.get(pk=version.pk)
    assert reloaded.scoring == VALID_SCORING


@pytest.mark.django_db
def test_published_type_key_cannot_be_changed(question, registry):
    version = _create_version(question)
    publish_question_version(version, registry)

    version.type_key = "different_type"
    with pytest.raises(ValueError):
        version.save()

    reloaded = QuestionVersion.objects.get(pk=version.pk)
    assert reloaded.type_key == TYPE_KEY


@pytest.mark.django_db
def test_published_version_number_cannot_be_silently_changed(question, registry):
    version = _create_version(question)
    publish_question_version(version, registry)

    version.version = 999
    with pytest.raises(ValueError):
        version.save()

    reloaded = QuestionVersion.objects.get(pk=version.pk)
    assert reloaded.version == 1


# --- lookup -----------------------------------------------------------


@pytest.mark.django_db
def test_get_question_version_finds_published_exact_version(question, registry):
    version = _create_version(question)
    publish_question_version(version, registry)

    found = get_question_version(question.public_id, 1)

    assert found.pk == version.pk


@pytest.mark.django_db
def test_get_question_version_rejects_wrong_version(question, registry):
    version = _create_version(question)
    publish_question_version(version, registry)

    with pytest.raises(QuestionVersion.DoesNotExist):
        get_question_version(question.public_id, 2)


@pytest.mark.django_db
def test_get_question_version_rejects_wrong_question_uuid(question, registry):
    version = _create_version(question)
    publish_question_version(version, registry)

    with pytest.raises(QuestionVersion.DoesNotExist):
        get_question_version(uuid.uuid4(), 1)


@pytest.mark.django_db
def test_get_question_version_does_not_return_unpublished_version(question):
    _create_version(question)

    with pytest.raises(QuestionVersion.DoesNotExist):
        get_question_version(question.public_id, 1)


# --- learner-safe serialization ------------------------------------------


@pytest.mark.django_db
def test_serialize_question_for_learner_includes_identity_and_payload(question, registry):
    version = _create_version(question)
    published = publish_question_version(version, registry)

    payload = serialize_question_for_learner(published, registry)

    assert payload["question_public_id"] == str(question.public_id)
    assert payload["question_version"] == 1
    assert payload["type"] == "single_choice"
    assert payload["prompt"] == VALID_CONTENT["prompt"]
    assert payload["options"] == VALID_CONTENT["options"]


@pytest.mark.django_db
def test_serialize_question_for_learner_requires_published_version(question, registry):
    version = _create_version(question)

    with pytest.raises(ValueError):
        serialize_question_for_learner(version, registry)


@pytest.mark.django_db
def test_serialize_question_for_learner_never_leaks_answer_key(question, registry):
    version = _create_version(question)
    published = publish_question_version(version, registry)

    payload = serialize_question_for_learner(published, registry)
    serialized = json.dumps(payload)

    assert "answer_key" not in payload
    assert "correct_option_id" not in payload
    assert "scoring" not in payload
    assert set(payload.keys()) == {"question_public_id", "question_version", "type", "prompt", "options"}
    assert "answer_key" not in serialized
    assert "correct_option_id" not in serialized
    assert "scoring" not in serialized


@pytest.mark.django_db
def test_serialize_question_for_learner_never_leaks_scoring(question, registry):
    version = _create_version(question, scoring={"max_score": 7})
    published = publish_question_version(version, registry)

    payload = serialize_question_for_learner(published, registry)

    assert "max_score" not in payload
    assert "scoring" not in payload


# --- provider payload identity collision guard (Task 06 corrective) ------


class _HostileProvider:
    type_key = "hostile"

    def validate(self, content, answer_key, scoring):
        pass

    def learner_payload(self, content):
        # attempts to override the wrapper's identity fields
        return {"type": "hostile", "question_public_id": "attacker-controlled", "prompt": "hi"}

    def evaluate(self, content, answer_key, scoring, response):
        return {"correct": False, "score": 0, "max_score": 1}


@pytest.mark.django_db
def test_provider_payload_cannot_override_wrapper_identity_fields(question):
    hostile_registry = QuestionRegistry()
    hostile_registry.register(_HostileProvider())

    version = _create_version(question, type_key="hostile")
    published = publish_question_version(version, hostile_registry)

    with pytest.raises(ValueError):
        serialize_question_for_learner(published, hostile_registry)
