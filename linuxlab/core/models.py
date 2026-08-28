import uuid

from django.db import models


class StableIdentityModel(models.Model):
    """Abstract base providing a stable cross-instance identity.

    The local database primary key stays a private implementation detail;
    public_id is the UUID used for Central<->Node exchange and any other
    cross-boundary reference (ADR-007, ADR-011).
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        abstract = True


class DomainEvent(models.Model):
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event_type = models.CharField(max_length=255)
    aggregate_id = models.UUIDField(null=True, blank=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
