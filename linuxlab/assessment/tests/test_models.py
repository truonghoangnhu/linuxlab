import uuid
from typing import ClassVar

import pytest
from django import forms
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from linuxlab.assessment.models import Question, QuestionVersion

VALID_CONTENT = {
    "prompt": "Which command lists files?",
    "options": [
        {"id": "a", "text": "ls"},
        {"id": "b", "text": "rm"},
    ],
}
VALID_ANSWER_KEY = {"correct_option_id": "a"}
VALID_SCORING = {"max_score": 1}
TYPE_KEY = "single_choice"


def _make_version(question, version=1, **overrides):
    fields = {
        "question": question,
        "version": version,
        "type_key": TYPE_KEY,
        "content": VALID_CONTENT,
        "answer_key": VALID_ANSWER_KEY,
        "scoring": VALID_SCORING,
    }
    fields.update(overrides)
    return QuestionVersion(**fields)


@pytest.mark.django_db
def test_question_receives_uuid_public_id():
    question = Question.objects.create(name="Shell basics quiz item")

    assert isinstance(question.public_id, uuid.UUID)


@pytest.mark.django_db
def test_two_questions_receive_distinct_public_ids():
    first = Question.objects.create(name="Q1")
    second = Question.objects.create(name="Q2")

    assert first.public_id != second.public_id


@pytest.mark.django_db
def test_question_retains_local_integer_primary_key():
    question = Question.objects.create(name="Q1")

    assert isinstance(question.pk, int)
    assert str(question.public_id) != str(question.pk)


def test_question_has_no_type_key_field():
    # Task 06 correction: provider type belongs to QuestionVersion, not the
    # mutable Question container, so it can never drift out from under an
    # already-published version.
    assert not hasattr(Question, "type_key")


@pytest.mark.django_db
def test_first_question_version_can_be_created_directly():
    question = Question.objects.create(name="Q1")

    version = _make_version(question, version=1)
    version.save()

    assert version.version == 1
    assert version.type_key == TYPE_KEY
    assert version.published_at is None


@pytest.mark.django_db
def test_question_version_type_key_is_not_globally_unique():
    question = Question.objects.create(name="Q1")
    other_question = Question.objects.create(name="Q2")

    first = _make_version(question, version=1)
    first.save()
    second = _make_version(other_question, version=1)
    second.save()

    assert first.type_key == second.type_key == TYPE_KEY


@pytest.mark.django_db
def test_unique_question_and_version_is_enforced():
    question = Question.objects.create(name="Q1")
    _make_version(question, version=1).save()

    with pytest.raises(IntegrityError):
        _make_version(question, version=1).save()


@pytest.mark.django_db
def test_different_questions_may_each_have_their_own_version_one():
    first = Question.objects.create(name="Q1")
    second = Question.objects.create(name="Q2")

    first_version = _make_version(first, version=1)
    first_version.save()
    second_version = _make_version(second, version=1)
    second_version.save()

    assert first_version.version == second_version.version == 1


@pytest.mark.django_db
def test_content_answer_key_and_scoring_remain_separate_and_persist_exactly():
    question = Question.objects.create(name="Q1")

    version = _make_version(question, version=1)
    version.save()
    reloaded = QuestionVersion.objects.get(pk=version.pk)

    assert reloaded.content == VALID_CONTENT
    assert reloaded.answer_key == VALID_ANSWER_KEY
    assert reloaded.scoring == VALID_SCORING
    assert "correct_option_id" not in reloaded.content
    assert "answer_key" not in reloaded.content
    assert "scoring" not in reloaded.content


@pytest.mark.django_db
def test_question_version_is_published_reflects_published_at():
    question = Question.objects.create(name="Q1")
    version = _make_version(question, version=1)
    version.save()

    assert version.is_published is False


# --- version >= 1 -----------------------------------------------------


@pytest.mark.django_db
def test_version_zero_is_rejected_by_model_validation():
    question = Question.objects.create(name="Q1")
    version = _make_version(question, version=0)

    with pytest.raises(ValidationError):
        version.full_clean()


@pytest.mark.django_db
def test_version_zero_is_rejected_by_the_database_constraint():
    question = Question.objects.create(name="Q1")

    with pytest.raises(IntegrityError), transaction.atomic():
        _make_version(question, version=0).save()

    assert QuestionVersion.objects.filter(question=question).count() == 0


@pytest.mark.django_db
def test_version_one_remains_valid():
    question = Question.objects.create(name="Q1")
    version = _make_version(question, version=1)

    version.full_clean()
    version.save()

    assert version.version == 1


# --- content/answer_key/scoring are required (not blank=True) -------------


class _QuestionVersionContentForm(forms.ModelForm):
    class Meta:
        model = QuestionVersion
        fields: ClassVar[list[str]] = ["type_key", "content", "answer_key", "scoring"]


def test_content_answer_key_and_scoring_are_required_on_modelform():
    form = _QuestionVersionContentForm()

    assert form.fields["content"].required is True
    assert form.fields["answer_key"].required is True
    assert form.fields["scoring"].required is True


@pytest.mark.django_db
def test_missing_content_is_rejected_by_full_clean():
    question = Question.objects.create(name="Q1")
    version = QuestionVersion(
        question=question,
        version=1,
        type_key=TYPE_KEY,
        answer_key=VALID_ANSWER_KEY,
        scoring=VALID_SCORING,
    )

    with pytest.raises(ValidationError):
        version.full_clean()


@pytest.mark.django_db
def test_missing_scoring_is_rejected_by_full_clean():
    question = Question.objects.create(name="Q1")
    version = QuestionVersion(
        question=question,
        version=1,
        type_key=TYPE_KEY,
        content=VALID_CONTENT,
        answer_key=VALID_ANSWER_KEY,
    )

    with pytest.raises(ValidationError):
        version.full_clean()


# --- published immutability covers the new fields too ----------------------


@pytest.mark.django_db
def test_published_type_key_cannot_be_changed():
    question = Question.objects.create(name="Q1")
    version = _make_version(question, version=1)
    version.save()
    version.published_at = timezone.now()
    version.save()

    version.type_key = "different_type"
    with pytest.raises(ValueError):
        version.save()

    reloaded = QuestionVersion.objects.get(pk=version.pk)
    assert reloaded.type_key == TYPE_KEY


@pytest.mark.django_db
def test_published_scoring_cannot_be_changed():
    question = Question.objects.create(name="Q1")
    version = _make_version(question, version=1)
    version.save()
    version.published_at = timezone.now()
    version.save()

    version.scoring = {"max_score": 5}
    with pytest.raises(ValueError):
        version.save()

    reloaded = QuestionVersion.objects.get(pk=version.pk)
    assert reloaded.scoring == VALID_SCORING
