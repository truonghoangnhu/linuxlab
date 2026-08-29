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

    for version in QuestionVersion.objects.select_related("question").iterator():
        version.type_key = version.question.type_key
        version.scoring = {"max_score": 1}
        version.save(update_fields=["type_key", "scoring"])


def backfill_question_type_key(apps, schema_editor):
    """Best-effort reverse: restore each Question.type_key from its most
    recent QuestionVersion (versions may legitimately diverge in type_key
    going forward, so this is a lossy but reasonable reconstruction for a
    migration that is not expected to run backward in normal operation).
    """
    Question = apps.get_model("assessment", "Question")

    for question in Question.objects.iterator():
        latest_version = question.versions.order_by("-version").first()
        if latest_version is not None:
            question.type_key = latest_version.type_key
            question.save(update_fields=["type_key"])


class Migration(migrations.Migration):

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
        # Stage 2: re-add Question.type_key as nullable temporarily so the
        # reverse migration has somewhere to write the reconstructed value;
        # forward, this is immediately backfilled from itself (a no-op) and
        # then removed in stage 4.
        migrations.AlterField(
            model_name="question",
            name="type_key",
            field=models.CharField(max_length=100, null=True),
        ),
        # Stage 3: backfill from the historical models, then require the
        # fields going forward.
        migrations.RunPython(
            backfill_type_key_and_scoring,
            reverse_code=backfill_question_type_key,
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
        # Stage 4: Question.type_key is no longer authoritative now that
        # every QuestionVersion carries its own frozen type_key.
        migrations.RemoveField(
            model_name="question",
            name="type_key",
        ),
    ]
