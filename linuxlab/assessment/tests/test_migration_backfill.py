import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

APP = "assessment"
BEFORE = [(APP, "0001_initial")]
AFTER = [(APP, "0002_freeze_question_version_semantics")]


@pytest.mark.django_db(transaction=True)
def test_migration_0002_backfills_type_key_and_scoring_from_question():
    executor = MigrationExecutor(connection)

    # Start from the state just after 0001_initial, using historical models
    # (never the live runtime models) to create rows exactly as they would
    # have existed under the pre-correction schema.
    executor.migrate(BEFORE)
    executor.loader.build_graph()

    old_state = executor.loader.project_state(BEFORE)
    old_apps = old_state.apps
    OldQuestion = old_apps.get_model(APP, "Question")
    OldQuestionVersion = old_apps.get_model(APP, "QuestionVersion")

    question = OldQuestion.objects.create(name="Legacy question", type_key="single_choice")
    version = OldQuestionVersion.objects.create(
        question=question,
        version=1,
        content={
            "prompt": "Which command lists files?",
            "options": [
                {"id": "a", "text": "ls"},
                {"id": "b", "text": "rm"},
            ],
        },
        answer_key={"correct_option_id": "a"},
    )
    original_question_pk = question.pk
    original_question_public_id = question.public_id
    original_version_pk = version.pk

    try:
        # Migrate forward through 0002; this exercises the real RunPython
        # backfill against real PostgreSQL data.
        executor.migrate(AFTER)
        executor.loader.build_graph()

        new_state = executor.loader.project_state(AFTER)
        new_apps = new_state.apps
        NewQuestion = new_apps.get_model(APP, "Question")
        NewQuestionVersion = new_apps.get_model(APP, "QuestionVersion")

        migrated_version = NewQuestionVersion.objects.get(pk=original_version_pk)
        assert migrated_version.type_key == "single_choice"
        assert migrated_version.scoring == {"max_score": 1}
        assert migrated_version.pk == original_version_pk
        assert migrated_version.question_id == original_question_pk
        assert migrated_version.content == {
            "prompt": "Which command lists files?",
            "options": [
                {"id": "a", "text": "ls"},
                {"id": "b", "text": "rm"},
            ],
        }
        assert migrated_version.answer_key == {"correct_option_id": "a"}

        migrated_question = NewQuestion.objects.get(pk=original_question_pk)
        assert migrated_question.public_id == original_question_public_id
        assert not hasattr(NewQuestion, "type_key")
        assert "type_key" not in [f.name for f in NewQuestion._meta.get_fields()]
    finally:
        # Restore the schema to the real migration leaf so the rest of the
        # suite runs against the expected, fully-migrated database.
        executor.loader.build_graph()
        leaf = executor.loader.graph.leaf_nodes(APP)
        executor.migrate(leaf)
