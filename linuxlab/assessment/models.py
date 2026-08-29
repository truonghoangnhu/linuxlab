from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q

from linuxlab.core.models import StableIdentityModel


class Question(StableIdentityModel):
    """A question identity/container (ADR-013). Question is mutable metadata
    only; it must never own interpretation semantics such as provider type,
    content, grading, or scoring. Those belong entirely to QuestionVersion,
    so that a published version's meaning can never drift out from under it
    (Task 06 correction).
    """

    name = models.CharField(max_length=255)


class QuestionVersion(models.Model):
    """An exact, eventually-immutable version of one Question.

    Distributed identity is Question.public_id + version; this model
    intentionally has no public_id of its own (ADR-011, ADR-014).

    type_key, content, answer_key, and scoring are ALL required (no default):
    an empty/underspecified question is never a valid domain state, unlike
    Task 05's Activity config/capabilities. type_key lives here (not on
    Question) so the provider that interprets a published version can never
    change after the fact; version N+1 may legitimately use a different
    type_key than N, since that creates a new immutable version rather than
    reinterpreting an existing one.
    """

    question = models.ForeignKey(
        Question,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    type_key = models.CharField(max_length=100)
    content = models.JSONField()
    answer_key = models.JSONField()
    scoring = models.JSONField()
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["question", "version"],
                name="unique_question_version",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="question_version_number_gte_1",
            ),
        ]

    @property
    def is_published(self) -> bool:
        return self.published_at is not None

    def save(self, *args, **kwargs):
        # Every field on this model is part of the published snapshot, so once
        # the persisted row is published, any further save is rejected outright
        # rather than diffed field-by-field (ADR-014). The published-state
        # check and the write happen under the same row lock: an unlocked
        # SELECT-then-UPDATE would let a stale in-memory instance overwrite a
        # row published concurrently by another transaction (Task 05 lesson).
        if self.pk is None:
            super().save(*args, **kwargs)
            return

        with transaction.atomic():
            persisted_published_at = (
                QuestionVersion.objects.select_for_update()
                .filter(pk=self.pk)
                .values_list("published_at", flat=True)
                .first()
            )
            if persisted_published_at is not None:
                raise ValueError("published QuestionVersion is immutable")
            super().save(*args, **kwargs)
