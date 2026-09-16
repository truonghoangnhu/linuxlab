"""Typed sync configuration boundaries, independent of deployment side effects."""


def test_empty_retry_setting_defers_then_fails():
    from types import SimpleNamespace

    import pytest

    from linuxlab.sync.configuration import (
        SyncConfigurationError,
        load_sync_configuration,
        parse_sync_setting,
    )

    source = SimpleNamespace(
        LINUXLAB_SYNC_BACKOFF_BASE_SECONDS=parse_sync_setting(
            "LINUXLAB_SYNC_BACKOFF_BASE_SECONDS", ""
        ),
        LINUXLAB_SYNC_BACKOFF_CAP_SECONDS=3600.0,
        LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS=10,
        LINUXLAB_SYNC_RETRY_MAX_AGE=86400.0,
    )
    with pytest.raises(SyncConfigurationError):
        load_sync_configuration(settings_obj=source, purpose="retry")


import math
from types import SimpleNamespace
from uuid import UUID

import pytest

from linuxlab.sync import configuration as c

BASE = "LINUXLAB_SYNC_BACKOFF_BASE_SECONDS"
CAP = "LINUXLAB_SYNC_BACKOFF_CAP_SECONDS"
ATTEMPTS = "LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS"
AGE = "LINUXLAB_SYNC_RETRY_MAX_AGE"
ENABLED = "LINUXLAB_SYNC_ENABLED"
PROBE = "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE"


def source(**changes):
    values = {
        "LINUXLAB_CAN_RUN_SYNC_WORKER": True,
        "LINUXLAB_SYNC_ENABLED": True,
        "LINUXLAB_NODE_ID": "b3f6a6e2-9b1a-4e3e-9f2a-8f6a2f1c4a10",
        "LINUXLAB_SYNC_SECRET": "SECRET_SENTINEL",
        "LINUXLAB_CENTRAL_URL": "https://central.example.org",
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "name,raw",
    [
        (BASE, v)
        for v in ("", " ", "bad", True, None, 0, -1, math.nan, math.inf, "nan", "inf", 1e300)
    ]
    + [(ATTEMPTS, v) for v in (True, 1.0, "1.5", "", 0, 2147483648)]
    + [(ENABLED, v) for v in ("yes", "1", 1, None, "")],
)
def test_parser_defers_invalid_values(name, raw):
    result = c.parse_sync_setting(name, raw)
    assert isinstance(result, c.DeferredSyncSettingError)
    with pytest.raises(c.SyncConfigurationError):
        bool(result)


@pytest.mark.parametrize(
    "name,raw,expected",
    [
        (ENABLED, " TrUe ", True),
        (ENABLED, " FALSE ", False),
        (ENABLED, False, False),
        (BASE, "1.25", 1.25),
        (BASE, 2, 2.0),
        (ATTEMPTS, "2147483647", 2147483647),
        (ATTEMPTS, 1, 1),
    ],
)
def test_parser_valid_typed_values(name, raw, expected):
    value = c.parse_sync_setting(name, raw)
    assert value == expected
    assert type(value) is type(expected)


def test_defaults_and_worker_fields():
    retry = c.load_sync_configuration(settings_obj=SimpleNamespace())
    assert retry.retry_policy == c.SyncRetryPolicy(5.0, 3600.0, 10, 86400.0)
    assert retry.enabled is False and retry.require_central_probe is False
    assert retry.credentials is None and retry.batch_max_events is None
    worker = c.load_sync_configuration(settings_obj=source(), purpose="worker")
    assert (worker.batch_max_events, worker.batch_max_bytes, worker.lease_seconds) == (
        100,
        1000000,
        300,
    )
    assert worker.credentials.node_id == UUID(source().LINUXLAB_NODE_ID)


@pytest.mark.parametrize(
    "changes",
    [{BASE: ""}, {BASE: 5, CAP: 5}, {BASE: 7, CAP: 5}, {AGE: 3e11}, {CAP: 3e11}, {ATTEMPTS: False}],
)
def test_invalid_required_policy(changes):
    with pytest.raises(c.SyncConfigurationError):
        c.load_sync_configuration(settings_obj=source(**changes))


@pytest.mark.parametrize("purpose", ["worker", "probe"])
def test_denied_capability(purpose):
    with pytest.raises(c.SyncConfigurationError):
        c.load_sync_configuration(
            settings_obj=source(LINUXLAB_CAN_RUN_SYNC_WORKER=False), purpose=purpose
        )


def test_purpose_isolation():
    broken = {
        ENABLED: "bad",
        PROBE: "bad",
        "LINUXLAB_NODE_ID": None,
        "LINUXLAB_SYNC_BATCH_MAX_EVENTS": "bad",
    }
    assert c.load_sync_configuration(settings_obj=source(**broken)).retry_policy.max_attempts == 10
    disabled = source(**{ENABLED: False, BASE: "bad", "LINUXLAB_NODE_ID": None})
    release = c.load_sync_configuration(settings_obj=disabled, purpose="release")
    assert release.retry_policy is None and release.credentials is None
    assert release.batch_max_events is None
    with pytest.raises(c.SyncConfigurationError):
        c.load_sync_configuration(settings_obj=disabled, purpose="worker")
    central = source(**{ENABLED: "bad", PROBE: "bad", "LINUXLAB_CAN_RUN_SYNC_WORKER": False})
    assert c.load_sync_configuration(settings_obj=central, purpose="release").credentials is None
    assert c.load_inbound_limits(settings_obj=central) == (100, 1000000)
    probe = c.load_sync_configuration(
        settings_obj=source(**{ENABLED: "bad", PROBE: "bad"}), purpose="probe"
    )
    assert probe.credentials is not None and probe.enabled is False


@pytest.mark.parametrize(
    "changes",
    [
        {PROBE: "bad"},
        {ENABLED: "bad"},
        {ENABLED: False, PROBE: True, "LINUXLAB_NODE_ID": None},
        {ENABLED: True, BASE: "bad"},
    ],
)
def test_release_required_configuration_fails(changes):
    with pytest.raises(c.SyncConfigurationError):
        c.load_sync_configuration(settings_obj=source(**changes), purpose="release")


def test_release_probe_disabled_sync_and_identity_independent_url():
    config = c.load_sync_configuration(
        settings_obj=source(**{ENABLED: False, PROBE: True}), purpose="release"
    )
    assert config.enabled is False and config.require_central_probe is True
    assert config.credentials is not None and config.retry_policy is not None
    assert c.load_node_identity(settings_obj=source(LINUXLAB_CENTRAL_URL=None)) == UUID(
        source().LINUXLAB_NODE_ID
    )


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "ftp://central",
        "https://",
        "http://central",
        "https://user:password@central",
        "https://central/#secret",
        "https://central:invalid",
        "https://[bad",
    ],
)
def test_credentials_reject_invalid_urls(url):
    with pytest.raises(c.SyncConfigurationError):
        c.load_node_credentials(settings_obj=source(LINUXLAB_CENTRAL_URL=url))


def test_explicit_http_opt_in_and_repr_safety():
    credentials = c.load_node_credentials(
        settings_obj=source(
            LINUXLAB_CENTRAL_URL="http://URL_SENTINEL", LINUXLAB_SYNC_ALLOW_INSECURE_HTTP="true"
        )
    )
    assert credentials.allow_insecure_http is True
    assert "SECRET_SENTINEL" not in repr(credentials)
    assert "URL_SENTINEL" not in repr(credentials)
    assert "SECRET_SENTINEL" not in repr(
        c.load_sync_configuration(settings_obj=source(), purpose="worker")
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"LINUXLAB_NODE_ID": "bad"},
        {"LINUXLAB_NODE_ID": None},
        {"LINUXLAB_SYNC_SECRET": " "},
        {"LINUXLAB_SYNC_SECRET": 12},
    ],
)
def test_identity_requires_uuid_and_nonblank_secret(changes):
    with pytest.raises(c.SyncConfigurationError):
        c.load_node_identity(settings_obj=source(**changes))


def test_safe_deferred_and_error_metadata():
    error = c.parse_sync_setting(BASE, "REJECTED_SENTINEL")
    assert "REJECTED_SENTINEL" not in repr(error)
    with pytest.raises(c.SyncConfigurationError) as caught:
        bool(error)
    assert "REJECTED_SENTINEL" not in str(caught.value)
    assert "REJECTED_SENTINEL" not in repr(caught.value.__dict__)
    for cls in (c.DeferredSyncSettingError, c.SyncConfigurationError):
        with pytest.raises(ValueError) as invalid:
            cls("REJECTED_SENTINEL", "SECRET_SENTINEL")
        assert "SENTINEL" not in str(invalid.value)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "name", ["LINUXLAB_SYNC_BATCH_MAX_EVENTS", "LINUXLAB_SYNC_BATCH_MAX_BYTES"]
)
def test_inbound_invalid_limit_is_safe_503(client, settings, name, caplog):
    from unittest.mock import patch

    from linuxlab.sync.authentication import issue_node_registration
    from linuxlab.sync.models import InboxMessage

    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("configuration test")
    setattr(settings, name, "REJECTED_SENTINEL")
    before = list(InboxMessage.objects.values())
    with patch("linuxlab.sync.api.ingest_event") as ingest:
        response = client.post(
            "/api/v1/sync/events/",
            data='{"events":[]}',
            content_type="application/json",
            HTTP_X_LINUXLAB_NODE_ID=str(credential.node_id),
            HTTP_AUTHORIZATION=f"Bearer {credential.secret}",
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "sync configuration unavailable"}
    ingest.assert_not_called()
    assert list(InboxMessage.objects.values()) == before
    assert "REJECTED_SENTINEL" not in response.content.decode() + caplog.text


def test_legacy_twice_cap_timestamp_must_fit():
    # Existing singleton-400 scheduling still uses twice cap until Task 8.
    with pytest.raises(c.SyncConfigurationError):
        c.load_sync_configuration(settings_obj=source(**{CAP: 150_000_000_000.0}))


def test_pure_import_and_defaults_without_django():
    import subprocess
    import sys

    script = """
import importlib.abc
import sys
from types import SimpleNamespace
class NoDjango(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "django" or fullname.startswith("django.") or fullname == "linuxlab.sync.models":
            raise AssertionError("eager deployment dependency")
sys.meta_path.insert(0, NoDjango())
from linuxlab.sync.configuration import load_sync_configuration
assert load_sync_configuration(settings_obj=SimpleNamespace()).retry_policy.max_attempts == 10
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "name",
    [
        "LINUXLAB_SYNC_BATCH_MAX_EVENTS",
        "LINUXLAB_SYNC_BATCH_MAX_BYTES",
        "LINUXLAB_SYNC_LEASE_SECONDS",
    ],
)
@pytest.mark.parametrize("value", [False, 1.0, 0, -1, 2147483648, ""])
def test_integer_limits_fail_closed(name, value):
    with pytest.raises(c.SyncConfigurationError):
        c.load_sync_configuration(settings_obj=source(**{name: value}), purpose="worker")


@pytest.mark.django_db
def test_inbound_limits_ignore_outbound_policy(client, settings):
    from linuxlab.sync.authentication import issue_node_registration

    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True
    credential = issue_node_registration("inbound isolation")
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = "invalid"
    settings.LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = "invalid"
    response = client.post(
        "/api/v1/sync/events/",
        data='{"events":[]}',
        content_type="application/json",
        HTTP_X_LINUXLAB_NODE_ID=str(credential.node_id),
        HTTP_AUTHORIZATION=f"Bearer {credential.secret}",
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}
