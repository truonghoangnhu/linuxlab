"""Task 15: wire LINUXLAB_SYNC_BACKOFF_BASE_SECONDS/CAP_SECONDS into
ordinary transient retry scheduling (closing the accepted Task 14
Minor: these settings existed but release_transient() used hardcoded
module constants instead).

Corrective B Task 8 removes the old deterministic-400/singleton long
protocol backoff mechanism entirely (`_release_singleton_protocol_fault`
no longer exists -- HTTP 400/PERMANENT_REJECTION dead-letters
immediately, uniformly, with no bisection and no long backoff of its
own); this file now covers only the ordinary transient full-jitter
backoff path and the pathological-configuration fail-closed behavior
shared by the whole retry policy.
"""

import math
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from linuxlab.sync.models import (
    OUTBOX_IN_FLIGHT,
    OUTBOX_PENDING,
    OutboxMessage,
    _insert_immutable_row,
)
from linuxlab.sync.outbox import BackoffConfigurationError, release_transient

pytestmark = pytest.mark.django_db


def _make_outbox(**overrides):
    now = timezone.now()
    fields = {
        "event_id": uuid.uuid4(),
        "domain_event": None,
        "event_type": "activity.completed",
        "schema_version": 1,
        "source_node_id": uuid.uuid4(),
        "occurred_at": now,
        "aggregate_type": "attempt",
        "aggregate_public_id": uuid.uuid4(),
        "payload": {"score": 1, "max_score": 1},
        "state": OUTBOX_IN_FLIGHT,
        "attempts": 0,
        "next_attempt_at": now,
        "claimed_at": now,
        "lease_until": now + timedelta(minutes=5),
        "claim_token": uuid.uuid4(),
        "delivered_at": None,
        "rejection_code": "",
        "rejection_reason": "",
    }
    fields.update(overrides)
    return _insert_immutable_row(OutboxMessage, **fields)


# --- ordinary transient retry honors configured base/cap -------------------


def test_release_transient_uses_configured_base_and_cap(settings):
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 2.0
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 8.0
    row = _make_outbox(attempts=0)
    now = timezone.now()

    with patch("linuxlab.sync.outbox.random.random", return_value=1.0):
        release_transient([row], "timeout", now=now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    # attempts becomes 1; n = max(1-1, 0) = 0 -> ceiling = base = 2
    assert refreshed.state == OUTBOX_PENDING
    assert refreshed.next_attempt_at == now + timedelta(seconds=2)


def test_release_transient_scales_with_larger_configured_values(settings):
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 20.0
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 80.0
    now = timezone.now()
    # attempts > 0 requires its real first-failure anchor (Fix Round 1, I4).
    row = _make_outbox(attempts=1, first_failure_at=now)

    with patch("linuxlab.sync.outbox.random.random", return_value=1.0):
        release_transient([row], "timeout", now=now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    # attempts becomes 2; n = max(2-1, 0) = 1 -> ceiling = min(80, 20*2) = 40
    assert refreshed.next_attempt_at == now + timedelta(seconds=40)


def test_release_transient_respects_configured_cap_at_high_attempts(settings):
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 2.0
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 8.0
    settings.LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS = 100
    row = _make_outbox(attempts=8, first_failure_at=timezone.now())
    now = timezone.now()

    with patch("linuxlab.sync.outbox.random.random", return_value=1.0):
        release_transient([row], "timeout", now=now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.next_attempt_at == now + timedelta(seconds=8)


# --- pathological configuration fails closed --------------------------------


@pytest.mark.parametrize(
    "base,cap",
    [
        (999.0, 5.0),  # base > cap
        (5.0, 5.0),  # base == cap
        (5.0, 0.0),  # cap not positive
        (5.0, -1.0),  # cap negative
        (0.0, 10.0),  # base not positive
        (-1.0, 10.0),  # base negative
        (math.nan, 10.0),  # base not finite
        (5.0, math.nan),  # cap not finite
        (5.0, math.inf),  # cap not finite
        (-math.inf, 10.0),  # base not finite
    ],
)
def test_release_transient_fails_closed_on_pathological_backoff_settings(settings, base, cap):
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = base
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = cap
    row = _make_outbox(attempts=0)

    with pytest.raises(BackoffConfigurationError):
        release_transient([row], "timeout")

    # Fail closed means the row is left untouched -- never silently
    # rescheduled for an immediate/past retry.
    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_IN_FLIGHT
    assert refreshed.attempts == 0


def test_release_transient_accepts_a_valid_boundary_adjacent_configuration(settings):
    """BASE just under CAP is valid -- only BASE >= CAP is rejected."""
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 4.999
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 5.0
    row = _make_outbox(attempts=0)

    release_transient([row], "timeout")  # does not raise

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_PENDING


def test_backoff_uses_canonical_error_and_retry_validation(settings):
    from linuxlab.sync.configuration import SyncConfigurationError
    from linuxlab.sync.outbox import _validated_backoff_bounds

    assert BackoffConfigurationError is SyncConfigurationError
    settings.LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS = True
    with pytest.raises(SyncConfigurationError):
        _validated_backoff_bounds()
