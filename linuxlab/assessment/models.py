from typing import ClassVar

from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q

from linuxlab.core.models import StableIdentityModel


class Question(StableIdentityModel):
    """A question identity (ADR-013). Content and grading data belong to
    QuestionVersion, not here. type_key is intentionally not unique: many
    Questions may use the same question-type provider.
    """

    name = models.CharField(max_length=255)
    type_key = models.CharField(max_length=100)


class QuestionVersion(models.Model):
    """An exact, eventually-immutable version of one Question.

    Distributed identity is Question.public_id + version; this model
    intentionally has no public_id of its own (ADR-011, ADR-014).

    content and answer_key are required (no default): an empty question is
    never a valid domain state, unlike Task 05's Activity config/capabilities.
    """

    question = models.ForeignKey(
        Question,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    content = models.JSONField()
    answer_key = models.JSONField()
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
