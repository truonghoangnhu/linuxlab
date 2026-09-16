"""Classify legacy evidence without guessing authority; intentionally irreversible."""

from django.db import migrations
from django.db.models import Count


def forwards(apps, schema_editor):
    Registration = apps.get_model("sync", "NodeRegistration")
    rows = Registration.objects.using(schema_editor.connection.alias)
    rows.update(state="revoked")
    candidates = rows.filter(enabled=True, revoked_at__isnull=True)
    unique_nodes = candidates.values("node_id").annotate(n=Count("pk")).filter(n=1)
    for group in unique_nodes.iterator():
        candidates.filter(node_id=group["node_id"]).update(state="active")


class Migration(migrations.Migration):
    dependencies = [("sync", "0005_registration_lifecycle_schema")]

    operations = [migrations.RunPython(forwards)]
