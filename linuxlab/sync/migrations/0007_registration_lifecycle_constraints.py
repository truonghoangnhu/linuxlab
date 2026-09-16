from django.db import migrations, models
from django.db.models import Count


def verify_lifecycle(apps, schema_editor):
    Registration = apps.get_model("sync", "NodeRegistration")
    rows = Registration.objects.using(schema_editor.connection.alias)
    if rows.filter(state__isnull=True).exists() or rows.exclude(
        state__in=["active", "superseded", "revoked"]
    ).exists():
        raise RuntimeError("Registration lifecycle verification failed: invalid state.")
    if rows.filter(state="active").values("node_id").annotate(n=Count("pk")).filter(n__gt=1).exists():
        raise RuntimeError("Registration lifecycle verification failed: multiple ACTIVE rows.")


class Migration(migrations.Migration):
    dependencies = [("sync", "0006_registration_lifecycle_data")]

    operations = [
        # Verification is read-only; structural reversal does not undo 0006.
        migrations.RunPython(verify_lifecycle, reverse_code=migrations.RunPython.noop),
        migrations.AlterField(
            model_name="noderegistration",
            name="state",
            field=models.CharField(
                max_length=10,
                choices=[("active", "Active"), ("superseded", "Superseded"), ("revoked", "Revoked")],
                default="revoked",
            ),
        ),
        migrations.AddConstraint(
            model_name="noderegistration",
            constraint=models.CheckConstraint(
                condition=models.Q(state__in=["active", "superseded", "revoked"]),
                name="node_registration_state_allowed_values",
            ),
        ),
        migrations.AddConstraint(
            model_name="noderegistration",
            constraint=models.UniqueConstraint(
                fields=["node_id"], condition=models.Q(state="active"),
                name="unique_active_node_registration_per_node",
            ),
        ),
    ]
