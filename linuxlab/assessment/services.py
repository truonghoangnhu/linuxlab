from django.db import transaction
from django.utils import timezone

from linuxlab.assessment.models import Question, QuestionVersion


def create_question_version(question, type_key, content, answer_key, scoring):
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
            type_key=type_key,
            content=content,
            answer_key=answer_key,
            scoring=scoring,
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

        # Provider is resolved from the exact version's own type_key, never
        # from the (mutable) Question, so a published version's grading
        # interpretation can never drift (Task 06 correction).
        provider = registry.get(locked_version.type_key)
        provider.validate(locked_version.content, locked_version.answer_key, locked_version.scoring)

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

    provider = registry.get(question_version.type_key)
    payload = provider.learner_payload(question_version.content)

    if "question_public_id" in payload or "question_version" in payload:
        raise ValueError(
            "provider learner_payload must not define question_public_id/question_version"
        )

    # Wrapper identity fields are added last so they can never be overridden
    # by provider payload content, even if the guard above were bypassed.
    return {
        **payload,
        "question_public_id": str(question_version.question.public_id),
        "question_version": question_version.version,
    }
