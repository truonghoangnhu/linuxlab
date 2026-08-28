from django.db import transaction
from django.utils import timezone

from linuxlab.assessment.models import Question, QuestionVersion


def create_question_version(question, content, answer_key):
    with transaction.atomic():
        locked_question = Question.objects.select_for_update().get(pk=question.pk)
        current_max = (
            QuestionVersion.objects.filter(question=locked_question)
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
        )
        next_version = (current_max or 0) + 1
        return QuestionVersion.objects.create(
            question=locked_question,
            version=next_version,
            content=content,
            answer_key=answer_key,
        )


def publish_question_version(question_version, registry):
    # Row lock must be acquired before the published-state check, otherwise
    # two concurrent publishers can both observe "unpublished" (Task 05
    # lesson). The locked, persisted row is what gets validated and published.
    with transaction.atomic():
        locked_version = (
            QuestionVersion.objects.select_for_update()
            .select_related("question")
            .get(pk=question_version.pk)
        )
        if locked_version.is_published:
            raise ValueError("published QuestionVersion is immutable")

        provider = registry.get(locked_version.question.type_key)
        provider.validate(locked_version.content, locked_version.answer_key)

        locked_version.published_at = timezone.now()
        locked_version.save()
        return locked_version


def get_question_version(question_public_id, version):
    return QuestionVersion.objects.get(
        question__public_id=question_public_id,
        version=version,
        published_at__isnull=False,
    )


def serialize_question_for_learner(question_version, registry):
    if not question_version.is_published:
        raise ValueError("QuestionVersion is not published")

    provider = registry.get(question_version.question.type_key)
    payload = provider.learner_payload(question_version.content)

    return {
        "question_public_id": str(question_version.question.public_id),
        "question_version": question_version.version,
        **payload,
    }
