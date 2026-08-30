"""Task 14 deployment-profile authority gating (corrective, independent
review finding #1: "Central-only authority is exposed on the Node
profile").

Central and Node are deployment profiles of one codebase (CLAUDE.md
invariant #3), not separate products -- but each profile is authorized
for a distinct half of the Task 14 protocol: Central accepts inbound
sync ingestion and provisions Node credentials; Node runs the outbound
sync worker. Both capabilities are read from one explicit, fail-closed
settings flag each -- the same pattern as LINUXLAB_CAN_AUTHOR/
LINUXLAB_CAN_IMPORT_PACKAGES/LINUXLAB_CAN_SERVE_LEARNER_CONTENT -- never
a LINUXLAB_PROFILE string comparison scattered across call sites.
"""

from django.conf import settings


def can_receive_sync_events():
    """Central-only: accept POST /api/v1/sync/events/ and provision/
    rotate/revoke NodeRegistration credentials."""
    return bool(getattr(settings, "LINUXLAB_CAN_RECEIVE_SYNC_EVENTS", False))


def can_run_sync_worker():
    """Node-only: run the outbound sync_events flush worker."""
    return bool(getattr(settings, "LINUXLAB_CAN_RUN_SYNC_WORKER", False))
