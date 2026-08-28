import uuid
from typing import ClassVar

import pytest
from django import forms
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from linuxlab.assessment.models import Question, QuestionVersion

VALID_CONTENT = {
    "prompt": "Which command lists files?",
    "options": [
        {"id": "a", "text": "ls"},
        {"id": "b", "text": "rm"},
    ],
}
VALID_ANSWER_KEY = {"correct_option_id": "a"}


@pytest.mark.django_db
def test_question_receives_uuid_public_id():
    question = Question.objects.create(name="Shell basics quiz item", type_key="single_choice")

    assert isinstance(question.public_id, uuid.UUID)


@pytest.mark.django_db
def test_two_questions_receive_distinct_public_ids():
    first = Question.objects.create(name="Q1", type_key="single_choice")
    second = Question.objects.create(name="Q2", type_key="single_choice")

    assert first.public_id != second.public_id


@pytest.mark.django_db
def test_question_retains_local_integer_primary_key():
    question = Question.objects.create(name="Q1", type_key="single_choice")

    assert isinstance(question.pk, int)
    assert str(question.public_id) != str(question.pk)


@pytest.mark.django_db
def test_type_key_is_not_globally_unique():
    first = Question.objects.create(name="Q1", type_key="single_choice")
    second = Question.objects.create(name="Q2", type_key="single_choice")

    assert first.type_key == second.type_key == "single_choice"


@pytest.mark.django_db
def test_first_question_version_can_be_created_directly():
    question = Question.objects.create(name="Q1", type_key="single_choice")

    version = QuestionVersion.objects.create(
        question=question, version=1, content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY
    )

    assert version.version == 1
    assert version.published_at is None


@pytest.mark.django_db
def test_unique_question_and_version_is_enforced():
    question = Question.objects.create(name="Q1", type_key="single_choice")
    QuestionVersion.objects.create(
        question=question, version=1, content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY
    )

    with pytest.raises(IntegrityError):
        QuestionVersion.objects.create(
            question=question, version=1, content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY
        )


@pytest.mark.django_db
def test_different_questions_may_each_have_their_own_version_one():
    first = Question.objects.create(name="Q1", type_key="single_choice")
    second = Question.objects.create(name="Q2", type_key="single_choice")

    first_version = QuestionVersion.objects.create(
        question=first, version=1, content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY
    )
    second_version = QuestionVersion.objects.create(
        question=second, version=1, content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY
    )

    assert first_version.version == second_version.version == 1


@pytest.mark.django_db
def test_content_and_answer_key_remain_separate_and_persist_exactly():
    question = Question.objects.create(name="Q1", type_key="single_choice")

    version = QuestionVersion.objects.create(
        question=question, version=1, content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY
    )
    reloaded = QuestionVersion.objects.get(pk=version.pk)

    assert reloaded.content == VALID_CONTENT
    assert reloaded.answer_key == VALID_ANSWER_KEY
    assert "correct_option_id" not in reloaded.content
    assert "answer_key" not in reloaded.content


@pytest.mark.django_db
def test_question_version_is_published_reflects_published_at():
    question = Question.objects.create(name="Q1", type_key="single_choice")
    version = QuestionVersion.objects.create(
        question=question, version=1, content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY
    )

    assert version.is_published is False


# --- version >= 1 -----------------------------------------------------


@pytest.mark.django_db
def test_version_zero_is_rejected_by_model_validation():
    question = Question.objects.create(name="Q1", type_key="single_choice")
    version = QuestionVersion(
        question=question, version=0, content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY
    )

    with pytest.raises(ValidationError):
        version.full_clean()


@pytest.mark.django_db
def test_version_zero_is_rejected_by_the_database_constraint():
    question = Question.objects.create(name="Q1", type_key="single_choice")

    with pytest.raises(IntegrityError), transaction.atomic():
        QuestionVersion.objects.create(
            question=question, version=0, content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY
        )

    assert QuestionVersion.objects.filter(question=question).count() == 0


@pytest.mark.django_db
def test_version_one_remains_valid():
    question = Question.objects.create(name="Q1", type_key="single_choice")
    version = QuestionVersion(
        question=question, version=1, content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY
    )

    version.full_clean()
    version.save()

    assert version.version == 1


# --- content/answer_key are required (not blank=True) ---------------------


class _QuestionVersionContentForm(forms.ModelForm):
    class Meta:
        model = QuestionVersion
        fields: ClassVar[list[str]] = ["content", "answer_key"]


def test_content_and_answer_key_are_required_on_modelform():
    form = _QuestionVersionContentForm()

    assert form.fields["content"].required is True
    assert form.fields["answer_key"].required is True


@pytest.mark.django_db
def test_missing_content_is_rejected_by_full_clean():
    question = Question.objects.create(name="Q1", type_key="single_choice")
    version = QuestionVersion(question=question, version=1, answer_key=VALID_ANSWER_KEY)

    with pytest.raises(ValidationError):
        version.full_clean()
