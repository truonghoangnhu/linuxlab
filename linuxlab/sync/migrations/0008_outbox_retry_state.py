"""Corrective B Task 7 (approved spec §13): add the three persisted
retry-state fields to OutboxMessage. OutboxMessage remains the sole
retry authority -- no parallel table, no new state vocabulary. Every
existing row already satisfies the new constraints via each field's
default (last_failure_category="" and last_http_status=NULL), so this
purely structural migration needs no accompanying data migration.
"""

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("sync", "0007_registration_lifecycle_constraints")]

    operations = [
        migrations.AddField(
            model_name="outboxmessage",
            name="first_failure_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="outboxmessage",
            name="last_failure_category",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "None"),
                    ("PERMANENT_REJECTION", "Permanent rejection"),
                    ("AUTH_OR_REGISTRATION_FAILURE", "Auth or registration failure"),
                    ("TRANSIENT_FAILURE", "Transient failure"),
                    ("RATE_LIMITED", "Rate limited"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="outboxmessage",
            name="last_http_status",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="outboxmessage",
            constraint=models.CheckConstraint(
                condition=Q(
                    last_failure_category__in=[
                        "",
                        "PERMANENT_REJECTION",
                        "AUTH_OR_REGISTRATION_FAILURE",
                        "TRANSIENT_FAILURE",
                        "RATE_LIMITED",
                    ]
                ),
                name="outbox_message_last_failure_category_allowed_values",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboxmessage",
            constraint=models.CheckConstraint(
                condition=(
                    Q(last_http_status__isnull=True)
                    | (Q(last_http_status__gte=100) & Q(last_http_status__lte=599))
                ),
                name="outbox_message_last_http_status_range",
            ),
        ),
    ]
