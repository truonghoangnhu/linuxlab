"""T15-D27 S5: existing Progress rows (all Central-style, created before
installed execution existed) are backfilled with their stable
activity_definition_public_id when migrating to 0002."""

import pytest
from django.db.migrations.executor import MigrationExecutor

APP = "learning"
BEFORE = [(APP, "0001_initial")]
AFTER = [(APP, "0002_t15d27_installed_execution_source")]


@pytest.mark.django_db(transaction=True)
def test_migration_0002_backfills_activity_definition_public_id():
    from django.db import connections

    executor = MigrationExecutor(connections["default"])

    executor.migrate(BEFORE)
    executor.loader.build_graph()

    old_state = executor.loader.project_state(BEFORE)
    old_apps = old_state.apps
    Learner = old_apps.get_model("identity", "Learner")
    ActivityDefinition = old_apps.get_model("activities", "ActivityDefinition")
    ActivityVersion = old_apps.get_model("activities", "ActivityVersion")
    Progress = old_apps.get_model(APP, "Progress")

    learner = Learner.objects.create(account=None)
    definition = ActivityDefinition.objects.create(name="Legacy activity", type_key="legacy")
    ActivityVersion.objects.create(definition=definition, version=1, config={})
    legacy_progress = Progress.objects.create(
        learner=learner, activity_definition=definition, completed=True, best_score=1
    )
    original_definition_public_id = definition.public_id

    executor.migrate(AFTER)
    executor.loader.build_graph()

    new_state = executor.loader.project_state(AFTER)
    new_apps = new_state.apps
    NewProgress = new_apps.get_model(APP, "Progress")

    migrated = NewProgress.objects.get(pk=legacy_progress.pk)
    assert migrated.activity_definition_public_id == original_definition_public_id
    assert migrated.activity_definition_id == definition.pk

    # Fully reversible (unlike assessment's 0002): restore the leaf so this
    # throwaway data does not leak into later tests on the same connection.
    executor.migrate(BEFORE)
