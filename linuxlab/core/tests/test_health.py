"""Task 15 liveness/readiness contract (T15-D15).

/health/ and /healthz share liveness: no DB query or configuration
disclosure. /ready/ and /readyz check DB connectivity, migration-state
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
    with patch("linuxlab.core.health.check_database", return_value=False):
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readyz_503_when_migrations_pending(client):
    with patch("linuxlab.core.health.check_schema_state", return_value=False):
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readyz_503_when_profile_invalid(client, settings):
    settings.LINUXLAB_PROFILE = "not-a-real-profile"

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readyz_503_when_required_profile_configuration_missing(client):
    with patch("linuxlab.core.health.check_profile_configuration", return_value=False):
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


# --- /readyz: response never leaks internal detail --------------------------


def test_readyz_response_never_contains_exception_or_config_detail(client):
    with patch(
        "linuxlab.core.health.check_database",
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


# --- /readyz: sync configuration is independent of local readiness --------


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


def test_readyz_ready_when_sync_enabled_and_node_id_missing(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = None
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_ready_when_sync_enabled_and_node_id_is_not_a_valid_uuid(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "not-a-uuid"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_ready_when_sync_enabled_and_secret_missing(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_ready_when_sync_enabled_and_secret_is_whitespace_only(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "   "
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_ready_when_sync_enabled_and_central_url_missing(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = None

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_ready_when_sync_enabled_and_central_url_has_no_scheme(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_ready_when_sync_enabled_and_central_url_has_wrong_scheme(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "ftp://central.example.org"

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_never_requires_node_sync_config_on_a_profile_that_cannot_run_the_worker(
    client, settings
):
    """Profiles without the sync worker also ignore Node-only sync settings."""
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = False
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = None
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_CENTRAL_URL = None

    response = client.get("/readyz")

    assert response.status_code == 200


def test_readyz_makes_no_external_http_request(client, settings):
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10"
    settings.LINUXLAB_SYNC_SECRET = "a-real-bearer-secret"
    settings.LINUXLAB_CENTRAL_URL = "https://central.example.org"

    with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Central offline")):
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


def test_ready_aliases_ignore_sync_configuration(client, settings):
    from linuxlab.core.health import check_database, check_schema_state

    assert check_database()
    assert check_schema_state()
    settings.LINUXLAB_PROFILE = "node"
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "invalid"
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = "invalid"
    responses = [client.get(path) for path in ("/ready/", "/readyz")]
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json() for response in responses] == [{"status": "ok"}] * 2


def test_public_local_primitives_when_healthy():
    from linuxlab.core.health import check_database, check_profile_configuration, check_schema_state

    assert check_database() is True
    assert check_profile_configuration() is True
    assert check_schema_state() is True


@pytest.mark.parametrize("profile", [None, "unknown", [], 12])
def test_schema_rejects_invalid_profile_before_inspection(settings, profile):
    from linuxlab.core import health

    settings.LINUXLAB_PROFILE = profile
    with patch.object(health, "MigrationExecutor") as inspect_schema:
        assert health.check_schema_state() is False
    inspect_schema.assert_not_called()


@pytest.mark.parametrize(
    "database",
    [
        {},
        {"default": {}},
        {"default": {"NAME": None}},
        {"default": {"NAME": ""}},
        {"default": {"NAME": "   "}},
        {"default": {"NAME": 42}},
        {"default": None},
        None,
    ],
)
def test_invalid_database_configuration_fails_without_inspection(client, monkeypatch, database):
    from types import SimpleNamespace

    from linuxlab.core import health

    # Replace the check's settings seam, not Django's live connection configuration.
    monkeypatch.setattr(
        health, "settings", SimpleNamespace(LINUXLAB_PROFILE="node", DATABASES=database)
    )
    assert health.check_profile_configuration() is False
    with patch.object(health, "MigrationExecutor") as inspect_schema:
        assert health.check_schema_state() is False
        for path in ("/ready/", "/readyz"):
            response = client.get(path)
            assert response.status_code == 503
            assert response.json() == {"status": "not_ready"}
    inspect_schema.assert_not_called()


@pytest.mark.parametrize(
    "path,view_name",
    [
        ("/health/", "healthz"),
        ("/healthz", "healthz"),
        ("/ready/", "readyz"),
        ("/readyz", "readyz"),
    ],
)
def test_direct_alias_routes(client, path, view_name):
    from django.urls import resolve

    from linuxlab.core import health

    assert resolve(path).func is getattr(health, view_name)
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "Location" not in response


@pytest.fixture
def forbidden_capability_calls():
    from contextlib import ExitStack

    targets = [
        "urllib.request.urlopen",
        "urllib.request.OpenerDirector.open",
        "linuxlab.publication.signing.load_package_signing_private_key",
        "linuxlab.sync.trust.load_package_trusted_public_key",
        "linuxlab.sync.authentication.authenticate_node",
        "linuxlab.sync.authentication.issue_node_registration",
        "linuxlab.sync.authentication.rotate_node_credential",
        "linuxlab.sync.authentication.revoke_node_registration",
        "linuxlab.sync.transports.HttpSyncTransport.send_batch",
        "linuxlab.sync.outbox.flush_outbox",
    ]
    with ExitStack() as stack:
        calls = [
            stack.enter_context(patch(target, side_effect=AssertionError("forbidden capability")))
            for target in targets
        ]
        yield
        for call in calls:
            call.assert_not_called()


@pytest.mark.parametrize("path", ["/health/", "/healthz"])
def test_liveness_isolated_from_all_checks(client, settings, path, forbidden_capability_calls):
    from contextlib import ExitStack

    from linuxlab.core import health

    settings.LINUXLAB_PROFILE = "invalid"
    with ExitStack() as stack:
        calls = [
            stack.enter_context(
                patch.object(health, name, side_effect=AssertionError("local check"))
            )
            for name in (
                "evaluate_readiness",
                "check_database",
                "check_schema_state",
                "check_profile_configuration",
            )
        ]
        with CaptureQueriesContext(connection) as queries:
            response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(queries) == 0
    for call in calls:
        call.assert_not_called()


@pytest.mark.parametrize("require_probe", [True, "invalid"])
def test_node_ready_offline_without_capability_checks(
    client, settings, require_probe, forbidden_capability_calls
):
    settings.LINUXLAB_PROFILE = "node"
    settings.LINUXLAB_CAN_RUN_SYNC_WORKER = True
    settings.LINUXLAB_SYNC_ENABLED = True
    settings.LINUXLAB_NODE_ID = "invalid"
    settings.LINUXLAB_SYNC_SECRET = None
    settings.LINUXLAB_CENTRAL_URL = "https://unavailable.example.invalid"
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = "invalid"
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = require_probe
    for path in ("/ready/", "/readyz"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("failure", ["database", "schema_exception", "pending_migration"])
def test_local_dependency_failures_are_safe_on_both_aliases(client, caplog, failure):
    from django.db import OperationalError

    from linuxlab.core import health

    sentinel = "PRIVATE_CONFIG_SENTINEL"
    if failure == "database":
        dependency = patch.object(
            connection, "ensure_connection", side_effect=OperationalError(sentinel)
        )
        primitive = health.check_database
    elif failure == "schema_exception":
        dependency = patch.object(health, "MigrationExecutor", side_effect=RuntimeError(sentinel))
        primitive = health.check_schema_state
    else:
        dependency = patch.object(
            health.MigrationExecutor, "migration_plan", return_value=[object()]
        )
        primitive = health.check_schema_state
    with dependency:
        assert primitive() is False
        responses = [client.get(path) for path in ("/ready/", "/readyz")]
    assert [response.status_code for response in responses] == [503, 503]
    assert [response.json() for response in responses] == [{"status": "not_ready"}] * 2
    assert all(sentinel not in response.content.decode() for response in responses)
    assert sentinel not in caplog.text


@pytest.mark.parametrize("profile", ["central", "node", "test"])
def test_schema_accepts_existing_profiles(settings, profile):
    from linuxlab.core.health import check_schema_state

    settings.LINUXLAB_PROFILE = profile
    assert check_schema_state() is True


def test_readiness_observes_schema_without_persistent_mutation(client, forbidden_capability_calls):
    from contextlib import ExitStack

    from django.contrib.sessions.models import Session

    from linuxlab.core.health import MigrationExecutor
    from linuxlab.core.models import DomainEvent
    from linuxlab.sync.models import NodeRegistration, OutboxMessage

    models = (DomainEvent, Session, NodeRegistration, OutboxMessage)
    before = {model: list(model.objects.order_by("pk").values()) for model in models}
    with ExitStack() as stack:
        writes = [
            stack.enter_context(
                patch.object(MigrationExecutor, name, side_effect=AssertionError("schema mutation"))
            )
            for name in ("migrate", "apply_migration", "unapply_migration")
        ]
        with CaptureQueriesContext(connection) as queries:
            for path in ("/ready/", "/readyz"):
                assert client.get(path).status_code == 200
    assert queries.captured_queries
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    for write in writes:
        write.assert_not_called()
    assert {model: list(model.objects.order_by("pk").values()) for model in models} == before
