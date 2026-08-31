"""Task 15 liveness/readiness contract (T15-D15).

/healthz is process liveness only: no DB query, no configuration
disclosure. /readyz checks DB connectivity, migration-state
compatibility, and active-profile validity -- it never mutates
schema/config and never discloses internal failure detail in the HTTP
response body (sanitized diagnostics belong in logs, not here).
"""

from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

pytestmark = pytest.mark.django_db


# --- /healthz ----------------------------------------------------------


def test_healthz_returns_200_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_makes_zero_database_queries(client):
    with CaptureQueriesContext(connection) as queries:
        client.get("/healthz")

    assert len(queries) == 0


def test_healthz_ignores_query_params_and_method_body(client):
    response = client.get("/healthz?probe=1")

    assert response.status_code == 200


# --- /readyz: success ----------------------------------------------------


def test_readyz_returns_200_ok_when_healthy(client):
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_success_for_central_like_profile(client, settings):
    settings.LINUXLAB_PROFILE = "central"

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_success_for_node_like_profile(client, settings):
    settings.LINUXLAB_PROFILE = "node"

    response = client.get("/readyz")

    assert response.status_code == 200


# --- /readyz: failure modes, each mocking only the failing dependency ------


def test_readyz_503_when_database_unavailable(client):
    with patch("linuxlab.core.health._check_database", return_value=False):
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readyz_503_when_migrations_pending(client):
    with patch("linuxlab.core.health._check_migrations_applied", return_value=False):
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readyz_503_when_profile_invalid(client, settings):
    settings.LINUXLAB_PROFILE = "not-a-real-profile"

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readyz_503_when_required_profile_configuration_missing(client):
    with patch("linuxlab.core.health._check_profile_configuration", return_value=False):
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


# --- /readyz: response never leaks internal detail --------------------------


def test_readyz_response_never_contains_exception_or_config_detail(client):
    with patch(
        "linuxlab.core.health._check_database",
        side_effect=Exception("connection to server at 10.0.0.5 failed: password authentication"),
    ):
        response = client.get("/readyz")

    body_text = response.content.decode()
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "10.0.0.5" not in body_text
    assert "password" not in body_text
    assert "Exception" not in body_text
    assert "Traceback" not in body_text


# --- /readyz: Node sync configuration (checkpoint corrective finding #4) ---


def test_readyz_success_when_sync_disabled_even_with_no_node_config(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = False
    settings.LINUXLAB_NODE_ID = None
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_CENTRAL_URL = None

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_success_when_sync_enabled_with_full_valid_node_config(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_503_when_sync_enabled_and_node_id_missing(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = None
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readyz_503_when_sync_enabled_and_node_id_is_not_a_valid_uuid(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "not-a-uuid"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 503


def test_readyz_503_when_sync_enabled_and_secret_missing(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 503


def test_readyz_503_when_sync_enabled_and_secret_is_whitespace_only(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "   "
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 503


def test_readyz_503_when_sync_enabled_and_central_url_missing(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = None

    response = client.get("/readyz")

    assert response.status_code == 503


def test_readyz_503_when_sync_enabled_and_central_url_has_no_scheme(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 503


def test_readyz_503_when_sync_enabled_and_central_url_has_wrong_scheme(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "ftp://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 503


def test_readyz_never_requires_node_sync_config_on_a_profile_that_cannot_run_the_worker(
    client, settings
):
    """Central must never require Node-only sync configuration, even if
    LINUXLAB_SYNC_ENABLED is somehow set -- gated on
    LINUXLAB_CAN_RUN_SYNC_WORKER (a fixed profile capability), not a
    LINUXLAB_PROFILE string comparison."""
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = False
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = None
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_CENTRAL_URL = None

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_sync_configuration_check_makes_no_external_http_request(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    with patch("socket.create_connection", side_effect=AssertionError("no network access")):
        response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_never_mutates_migration_state(client):
    """evaluate_readiness() must be read-only: it must never call
    Django's migrate executor's apply_migrations()/unapply_migrations(),
    only the read-only migration_plan()."""
    from linuxlab.core.health import evaluate_readiness

    with patch("django.db.migrations.executor.MigrationExecutor.migrate") as mutate:
        evaluate_readiness()

    mutate.assert_not_called()
