"""Corrective B Task 7 (approved spec §13): conservatively seed
first_failure_at for legacy rows already mid-retry-cycle when this
schema was introduced. The baseline recorded no first-failure
timestamp, so `created_at` is used as an UPPER-BOUND AGE ANCHOR --
never claimed to be the actual historical first-failure instant, never
migration-execution time, and never a fresh retry-age budget. Rows
with attempts=0 have never failed and keep first_failure_at NULL.
last_failure_category/last_http_status are left at their schema
defaults ("" / NULL) for all legacy rows -- unknown history is never
inferred. No state transition, no dead-lettering, and no other field is
touched.

Irreversible: after this runs there is no safe way to distinguish a
first_failure_at seeded from legacy created_at from a genuine value
recorded by a later real failure, so a reverse that cleared the field
would be inventing/discarding evidence rather than undoing a schema
change. This is a semantic irreversibility distinct from 0008's
ordinary structural schema reversal.
"""

from django.db import migrations
from django.db.models import F


def seed_first_failure_at(apps, schema_editor):
    Outbox = apps.get_model("sync", "OutboxMessage")
    Outbox.objects.using(schema_editor.connection.alias).filter(
        attempts__gt=0,
        first_failure_at__isnull=True,
    ).update(first_failure_at=F("created_at"))


class Migration(migrations.Migration):
    dependencies = [("sync", "0008_outbox_retry_state")]

    operations = [migrations.RunPython(seed_first_failure_at)]
