import contextlib
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


def _physical_columns(alias, table_name):
    """Actual PostgreSQL column names for table_name, via live catalog
    introspection - NOT the migration graph's declared state. project_state()
    can only describe what the migration *graph* says should exist; it says
    nothing about what PostgreSQL was actually left with after a failed
    reverse attempt.
    """
    connection = connections[alias]
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(cursor, table_name)
    return {field.name for field in description}


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


def _scratch_database():
    """Generator body for the scratch_db fixture below, factored out so the
    cleanup-completeness test can drive setup/teardown manually and inspect
    state in between - something a normal fixture can't do from within a
    single test function.

    Creates a fresh PostgreSQL database that has never had 0002 applied.
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
        # BaseConnectionHandler caches wrapper instances in a thread-local
        # (self._connections), separate from self.databases (== settings.
        # DATABASES). Closing the wrapper and removing the settings entry is
        # not enough: connections[alias] would still return the closed,
        # cached wrapper via that thread-local. __delitem__ is the public
        # API Django provides for exactly this (BaseConnectionHandler.
        # __delitem__ -> delattr(self._connections, alias)).
        del connections[alias]
        # connections.databases and settings.DATABASES are the same dict.
        del connections.databases[alias]
        with _admin_connect() as admin_conn, admin_conn.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def scratch_db():
    yield from _scratch_database()


def test_scratch_db_cleanup_removes_settings_entry_and_cached_connection():
    gen = _scratch_database()
    alias = next(gen)

    # Force the wrapper into ConnectionHandler's thread-local cache, exactly
    # as real usage (connections[alias]) does - no query/connect required.
    connections[alias]

    assert alias in connections.databases
    assert hasattr(connections._connections, alias)

    with contextlib.suppress(StopIteration):
        next(gen)  # drive the generator through its finally/teardown block

    assert alias not in connections.databases
    assert not hasattr(connections._connections, alias)


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

        # Assert the IMMEDIATE post-failure state, before any restoration:
        # a passing test here must prove no partial unapply occurred, not
        # merely that the leaf can be restored afterward.
        recorder = MigrationRecorder(connections["default"])
        assert (APP, "0002_freeze_question_version_semantics") in recorder.applied_migrations()

        columns = _physical_columns("default", "assessment_questionversion")
        assert "type_key" in columns
        assert "scoring" in columns
    finally:
        _restore_leaf(executor)


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

        # Assert the IMMEDIATE post-failure state, before any restoration.
        recorder = MigrationRecorder(connections["default"])
        assert (APP, "0002_freeze_question_version_semantics") in recorder.applied_migrations()

        columns = _physical_columns("default", "assessment_questionversion")
        assert "type_key" in columns
        assert "scoring" in columns
    finally:
        _restore_leaf(executor)
