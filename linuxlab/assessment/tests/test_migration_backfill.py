import uuid

import psycopg
import pytest
from django.conf import settings
from django.db import connections
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

APP = "assessment"
BEFORE = [(APP, "0001_initial")]
AFTER = [(APP, "0002_freeze_question_version_semantics")]


def _restore_leaf(executor):
    executor.loader.build_graph()
    leaf = executor.loader.graph.leaf_nodes(APP)
    executor.migrate(leaf)


def _admin_connect():
    db = settings.DATABASES["default"]
    return psycopg.connect(
        dbname="postgres",
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"],
        autocommit=True,
    )


@pytest.fixture
def scratch_db():
    """A fresh PostgreSQL database that has never had 0002 applied.

    0002 is deliberately irreversible (see its docstring), so a database
    already migrated to the leaf can no longer be moved backward to 0001 to
    set up "legacy" data. A separate, never-migrated-past-0001 database lets
    the real forward RunPython backfill be exercised without reversing
    anything.
    """
    name = f"test_migration_scratch_{uuid.uuid4().hex[:8]}"
    with _admin_connect() as admin_conn, admin_conn.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{name}"')

    alias = f"migration_scratch_{uuid.uuid4().hex[:8]}"
    settings.DATABASES[alias] = {**settings.DATABASES["default"], "NAME": name}

    try:
        yield alias
    finally:
        connections[alias].close()
        # connections.databases and settings.DATABASES are the same dict.
        del connections.databases[alias]
        with _admin_connect() as admin_conn, admin_conn.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_migration_0002_backfills_type_key_and_scoring_from_question(scratch_db, django_db_blocker):
    alias = scratch_db

    with django_db_blocker.unblock():
        executor = MigrationExecutor(connections[alias])

        # Migrate a fresh database forward to 0001_initial only, using
        # historical models (never the live runtime models) to create rows
        # exactly as they would have existed under the pre-correction schema.
        executor.migrate(BEFORE)
        executor.loader.build_graph()

        old_state = executor.loader.project_state(BEFORE)
        old_apps = old_state.apps
        OldQuestion = old_apps.get_model(APP, "Question")
        OldQuestionVersion = old_apps.get_model(APP, "QuestionVersion")

        question = OldQuestion.objects.using(alias).create(
            name="Legacy question", type_key="single_choice"
        )
        version = OldQuestionVersion.objects.using(alias).create(
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

        # Migrate forward through 0002; this exercises the real RunPython
        # backfill against real PostgreSQL data. Never reversed afterward:
        # this database is throwaway and dropped by the fixture.
        executor.migrate(AFTER)
        executor.loader.build_graph()

        new_state = executor.loader.project_state(AFTER)
        new_apps = new_state.apps
        NewQuestion = new_apps.get_model(APP, "Question")
        NewQuestionVersion = new_apps.get_model(APP, "QuestionVersion")

        migrated_version = NewQuestionVersion.objects.using(alias).get(pk=original_version_pk)
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

        migrated_question = NewQuestion.objects.using(alias).get(pk=original_question_pk)
        assert migrated_question.public_id == original_question_public_id
        assert not hasattr(NewQuestion, "type_key")
        assert "type_key" not in [f.name for f in NewQuestion._meta.get_fields()]


@pytest.mark.django_db(transaction=True)
def test_reverse_migration_is_irreversible_for_a_zero_version_question():
    # A Question with no QuestionVersion rows has no version-level type_key
    # from which the old required Question.type_key could be reconstructed.
    executor = MigrationExecutor(connections["default"])
    executor.migrate(AFTER)
    executor.loader.build_graph()

    apps_at_0002 = executor.loader.project_state(AFTER).apps
    Question = apps_at_0002.get_model(APP, "Question")
    Question.objects.create(name="No versions yet")

    try:
        with pytest.raises(IrreversibleError):
            executor.migrate(BEFORE)
    finally:
        _restore_leaf(executor)

    recorder = MigrationRecorder(connections["default"])
    assert (APP, "0002_freeze_question_version_semantics") in recorder.applied_migrations()

    apps_after_attempt = executor.loader.project_state(AFTER).apps
    QuestionVersion = apps_after_attempt.get_model(APP, "QuestionVersion")
    field_names = {f.name for f in QuestionVersion._meta.get_fields()}
    assert "type_key" in field_names
    assert "scoring" in field_names


@pytest.mark.django_db(transaction=True)
def test_reverse_migration_is_irreversible_for_divergent_provider_versions():
    # Two versions of the same Question using different type_key values
    # cannot be losslessly collapsed into one Question.type_key.
    executor = MigrationExecutor(connections["default"])
    executor.migrate(AFTER)
    executor.loader.build_graph()

    apps_at_0002 = executor.loader.project_state(AFTER).apps
    Question = apps_at_0002.get_model(APP, "Question")
    QuestionVersion = apps_at_0002.get_model(APP, "QuestionVersion")

    question = Question.objects.create(name="Divergent question")
    QuestionVersion.objects.create(
        question=question,
        version=1,
        type_key="single_choice",
        content={"prompt": "p", "options": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}]},
        answer_key={"correct_option_id": "a"},
        scoring={"max_score": 1},
    )
    QuestionVersion.objects.create(
        question=question,
        version=2,
        type_key="another_provider",
        content={"anything": "goes"},
        answer_key={"anything": "goes"},
        scoring={"max_score": 1},
    )

    try:
        with pytest.raises(IrreversibleError):
            executor.migrate(BEFORE)
    finally:
        _restore_leaf(executor)

    recorder = MigrationRecorder(connections["default"])
    assert (APP, "0002_freeze_question_version_semantics") in recorder.applied_migrations()
