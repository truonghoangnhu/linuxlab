from django.db import migrations, models


def backfill_type_key_and_scoring(apps, schema_editor):
    """Every existing QuestionVersion's interpretation semantics were, until
    now, resolved through its (mutable) Question.type_key. Freeze that
    exact, already-observed interpretation onto the version itself:
    type_key <- the owning Question's current type_key; scoring <- the
    binary {"max_score": 1} contract that was the only grading behavior
    QuestionVersion/SingleChoiceProvider implemented before this migration.
    """
    QuestionVersion = apps.get_model("assessment", "QuestionVersion")
    db_alias = schema_editor.connection.alias

    versions = QuestionVersion.objects.using(db_alias).select_related("question")
    for version in versions.iterator():
        version.type_key = version.question.type_key
        version.scoring = {"max_score": 1}
        version.save(using=db_alias, update_fields=["type_key", "scoring"])


class Migration(migrations.Migration):
    """Provider identity moves from Question (one mutable value) to
    QuestionVersion (frozen per version). Versions of the same Question may
    legitimately carry different type_key values going forward, and a
    Question with zero versions has no version-level type_key at all -
    neither case can be losslessly collapsed back into one required
    Question.type_key. This migration is therefore deliberately irreversible
    rather than pretending a lossy or arbitrary reverse mapping is safe.
    """

    dependencies = [
        ("assessment", "0001_initial"),
    ]

    operations = [
        # Stage 1: add the new columns nullable, so existing rows are not
        # rejected before they can be backfilled.
        migrations.AddField(
            model_name="questionversion",
            name="type_key",
            field=models.CharField(max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="questionversion",
            name="scoring",
            field=models.JSONField(null=True),
        ),
        # Stage 2: backfill from the historical models, then require the
        # fields going forward. Irreversible: see the migration docstring.
        migrations.RunPython(
            backfill_type_key_and_scoring,
            reverse_code=None,
        ),
        migrations.AlterField(
            model_name="questionversion",
            name="type_key",
            field=models.CharField(max_length=100),
        ),
        migrations.AlterField(
            model_name="questionversion",
            name="scoring",
            field=models.JSONField(),
        ),
        # Stage 3: Question.type_key is no longer authoritative now that
        # every QuestionVersion carries its own frozen type_key.
        migrations.RemoveField(
            model_name="question",
            name="type_key",
        ),
    ]
