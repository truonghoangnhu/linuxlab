import uuid

from django.db import migrations


def backfill_public_id(apps, schema_editor):
    Account = apps.get_model("identity", "Account")
    for account in Account.objects.filter(public_id__isnull=True).iterator():
        account.public_id = uuid.uuid4()
        account.save(update_fields=["public_id"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0002_account_public_id_nullable"),
    ]

    operations = [
        migrations.RunPython(backfill_public_id, noop_reverse),
    ]
