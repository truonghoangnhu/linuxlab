"""T15-D27 S5, upgraded by the T15-D27 corrective (Task 5, "representative
Attempt + Progress"): migrating a pre-T15-D27 database forward preserves
existing Central-style Attempts unchanged (installed_activity_version_id
NULL), backfills existing Progress rows with their stable
activity_definition_public_id, and leaves the new XOR CheckConstraint live
and enforced."""

import pytest
from django.db import IntegrityError, connections, transaction
from django.db.migrations.executor import MigrationExecutor

APP = "learning"
BEFORE = [(APP, "0001_initial")]
AFTER = [(APP, "0002_t15d27_installed_execution_source")]


def _restore_leaf(executor):
    """Mirrors linuxlab.assessment.tests.test_migration_backfill's own
    helper: this test migrates the SHARED "default" test database
    backward and forward in place (there is no scratch database here,
    since -- unlike assessment's 0002 -- this migration is fully
    reversible). Every exit path, including a mid-assertion failure,
    MUST leave migration state at the leaf; leaving it at BEFORE would
    silently strip installed_activity_version/activity_definition_public_id
    from every later test in the same session.
    """
    executor.loader.build_graph()
    leaf = executor.loader.graph.leaf_nodes(APP)
    executor.migrate(leaf)


@pytest.mark.django_db(transaction=True)
def test_migration_0002_preserves_attempts_and_backfills_progress():
    executor = MigrationExecutor(connections["default"])

    try:
        executor.migrate(BEFORE)
        executor.loader.build_graph()

        old_state = executor.loader.project_state(BEFORE)
        old_apps = old_state.apps
        Learner = old_apps.get_model("identity", "Learner")
        ActivityDefinition = old_apps.get_model("activities", "ActivityDefinition")
        ActivityVersion = old_apps.get_model("activities", "ActivityVersion")
        Attempt = old_apps.get_model(APP, "Attempt")
        Progress = old_apps.get_model(APP, "Progress")

        learner = Learner.objects.create(account=None)
        definition = ActivityDefinition.objects.create(name="Legacy activity", type_key="legacy")
        activity_version = ActivityVersion.objects.create(definition=definition, version=1, config={})
        legacy_attempt = Attempt.objects.create(
            learner=learner, activity_version=activity_version, submission=None, submitted_at=None
        )
        legacy_progress = Progress.objects.create(
            learner=learner, activity_definition=definition, completed=True, best_score=1
        )
        original_definition_public_id = definition.public_id

        executor.migrate(AFTER)
        executor.loader.build_graph()

        new_state = executor.loader.project_state(AFTER)
        new_apps = new_state.apps
        NewAttempt = new_apps.get_model(APP, "Attempt")
        NewProgress = new_apps.get_model(APP, "Progress")

        migrated_attempt = NewAttempt.objects.get(pk=legacy_attempt.pk)
        assert migrated_attempt.activity_version_id == activity_version.pk
        assert migrated_attempt.installed_activity_version_id is None

        migrated_progress = NewProgress.objects.get(pk=legacy_progress.pk)
        assert migrated_progress.activity_definition_public_id == original_definition_public_id
        assert migrated_progress.activity_definition_id == definition.pk

        # The new XOR CheckConstraint is live and enforced post-migration,
        # not merely declared: a row satisfying neither/both execution
        # sources must be rejected by PostgreSQL itself.
        with pytest.raises(IntegrityError), transaction.atomic():
            NewAttempt.objects.create(
                learner_id=learner.pk, activity_version=None, installed_activity_version=None
            )
    finally:
        _restore_leaf(executor)
