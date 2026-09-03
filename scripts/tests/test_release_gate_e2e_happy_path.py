"""Task 15 Task 8: real dual-deployment happy-path E2E (T15-D02, T15-D09,
T15-D17, T15-D18, T15-D20, T15-D27).

The scenario itself lives in scripts.release_gate.scenarios.run_happy_path()
(Task 11 extraction) so this test and scripts/phase1_release_gate.py both
exercise the exact same real-infra logic -- two genuinely separate
ephemeral PostgreSQL databases, two real Gunicorn processes, real HTTP
throughout, an ephemeral Ed25519 signing key, a real signed `.llpkg`,
the locked production assessment/single_choice provider, a real issued
Node sync credential, and the real HttpSyncTransport delivering to
Central's real /api/v1/sync/events/ -- no Django test-client
substitute for the release scenario, no browser automation.
"""

from scripts.release_gate import scenarios
from scripts.release_gate.e2e_support import dual_deployment


def test_real_dual_deployment_happy_path(tmp_path):
    with dual_deployment(tmp_path) as deployment:
        scenarios.run_happy_path(deployment)
