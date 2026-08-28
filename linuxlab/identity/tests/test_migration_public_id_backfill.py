import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_account_public_id_backfill_is_safe_for_existing_rows():
    executor = MigrationExecutor(connection)

    # Roll back to the pre-public_id baseline (the already-applied,
    # already-reviewed Task 01 state) and create rows there, using the
    # HISTORICAL model for that state rather than the current runtime model.
    baseline = [("identity", "0001_initial")]
    executor.migrate(baseline)
    executor.loader.build_graph()
    baseline_state = executor.loader.project_state(baseline)
    HistoricalAccount = baseline_state.apps.get_model("identity", "Account")

    first = HistoricalAccount.objects.create(username="pre-existing-one", password="x")
    second = HistoricalAccount.objects.create(username="pre-existing-two", password="x")

    try:
        # Migrate forward through exactly the staged public_id rollout
        # (0002 nullable -> 0003 backfill -> 0004 required), stopping right
        # after it so this test isolates that migration's effect.
        required_state = [("identity", "0004_account_public_id_required")]
        executor.migrate(required_state)
        executor.loader.build_graph()
        new_state = executor.loader.project_state(required_state)
        HistoricalAccountAfterBackfill = new_state.apps.get_model("identity", "Account")

        migrated_first = HistoricalAccountAfterBackfill.objects.get(pk=first.pk)
        migrated_second = HistoricalAccountAfterBackfill.objects.get(pk=second.pk)

        assert migrated_first.public_id is not None
        assert migrated_second.public_id is not None
        assert migrated_first.public_id != migrated_second.public_id
        assert migrated_first.pk == first.pk
        assert migrated_second.pk == second.pk
    finally:
        # Always leave the test database at the latest migration state, even
        # if an assertion above fails, so later tests in the same run still
        # see the full expected schema.
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
