from django.contrib.auth.models import AbstractUser
from django.db import models

from linuxlab.core.models import StableIdentityModel


class Account(AbstractUser, StableIdentityModel):
    # public_id (UUID, unique, not editable) comes from StableIdentityModel.
    # Local integer id remains the database primary key; public_id is the
    # stable cross-instance identity (ADR-007, ADR-011). The column was
    # rolled out safely in three stages: 0002 (nullable) -> 0003 (distinct
    # backfill per row) -> 0004 (final not-null/unique/default state).
    pass


class Learner(StableIdentityModel):
    # Learning identity, distinct from Account (authentication identity).
    # A Learner may exist without a Central Account (ADR-012).
    account = models.OneToOneField(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="learner",
    )
    created_at = models.DateTimeField(auto_now_add=True)


class AnonymousLearner(StableIdentityModel):
    # Guest identity with no Account dependency. Task 03 defines the model
    # only; browser localStorage/claim wiring is Task 08.
    created_at = models.DateTimeField(auto_now_add=True)


class LearningContext(StableIdentityModel):
    # kind is a plain string, not an enum, so new context kinds (personal,
    # classroom, workshop, ...) don't require a future migration here.
    kind = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)


class RoleAssignment(StableIdentityModel):
    class Role(models.TextChoices):
        LEARNER = "learner", "Learner"
        CONTENT_EDITOR = "content_editor", "Content Editor"
        INSTRUCTOR = "instructor", "Instructor"
        EXAM_INVIGILATOR = "exam_invigilator", "Exam Invigilator"
        NODE_ADMINISTRATOR = "node_administrator", "Node Administrator"
        SYSTEM_ADMINISTRATOR = "system_administrator", "System Administrator"

    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )
    role = models.CharField(max_length=32, choices=Role.choices)
    # NULL context means a global assignment, not scoped to any one context.
    context = models.ForeignKey(
        LearningContext,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
