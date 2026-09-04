"""Task 15 Task 10: Central + Node backup/restore smoke (T15-D16, T15-D23),
using real `pg_dump`/`pg_restore` against real ephemeral PostgreSQL
databases -- never a scheduled backup service, never a bundled secrets+DB
artifact. The scenario itself lives in
scripts.release_gate.scenarios.run_backup_restore_smoke() (Task 11
extraction) so this test and scripts/phase1_release_gate.py both
exercise the exact same real-infra logic. A successful `pg_restore`
alone is not treated as PASS: every assertion queries real
application-level state through the same release-gate action bridge
Tasks 7-9 use, against the restored databases specifically.
"""

from scripts.release_gate import scenarios
from scripts.release_gate.e2e_support import dual_deployment


def test_central_and_node_backup_restore_smoke(tmp_path):
    with dual_deployment(tmp_path) as deployment:
        detail = scenarios.run_backup_restore_smoke(deployment)

    # Task 15 Task 11 corrective v3, Section 13: backup_restore's
    # mandatory typed assertion set.
    assert detail["central_business_state_restored"] is True
    assert detail["node_business_state_restored"] is True
    assert detail["pending_outbox_restored"] is True
    assert detail["restored_sync_can_resume"] is True
