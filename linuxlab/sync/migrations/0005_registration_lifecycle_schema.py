import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("sync", "0004_outboxmessage_claim_token")]

    operations = [
        migrations.AddField(
            model_name="noderegistration",
            name="state",
            field=models.CharField(max_length=10, null=True),
        ),
        migrations.AlterField(
            model_name="noderegistration",
            name="node_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, db_index=True),
        ),
    ]
