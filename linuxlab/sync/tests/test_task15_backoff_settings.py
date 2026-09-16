"""Task 15: wire LINUXLAB_SYNC_BACKOFF_BASE_SECONDS/CAP_SECONDS into
ordinary transient retry scheduling (closing the accepted Task 14
Minor: these settings existed but release_transient() used hardcoded
module constants instead). T14-D41's singleton-400 long backoff stays
intentionally derived from the same (now-live) cap setting, exactly as
originally designed ("reuses the existing transient-backoff cap") --
it must track the cap when configured, but must never collapse to the
ordinary short/base-driven cadence.
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
from linuxlab.sync.outbox import (
    BackoffConfigurationError,
    _release_singleton_protocol_fault,
    release_transient,
)

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
    # attempts becomes 1; delay = min(cap=8, base=2 * 2**1=4) = 4
    assert refreshed.state == OUTBOX_PENDING
    assert refreshed.next_attempt_at == now + timedelta(seconds=4)


def test_release_transient_scales_with_larger_configured_values(settings):
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 20.0
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 80.0
    row = _make_outbox(attempts=0)
    now = timezone.now()

    with patch("linuxlab.sync.outbox.random.random", return_value=1.0):
        release_transient([row], "timeout", now=now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    # delay = min(cap=80, base=20 * 2**1=40) = 40
    assert refreshed.next_attempt_at == now + timedelta(seconds=40)


def test_release_transient_respects_configured_cap_at_high_attempts(settings):
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 2.0
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 8.0
    row = _make_outbox(attempts=100)
    now = timezone.now()

    with patch("linuxlab.sync.outbox.random.random", return_value=1.0):
        release_transient([row], "timeout", now=now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.next_attempt_at == now + timedelta(seconds=8)


# --- T14-D41 singleton 400 long backoff: tracks cap, never collapses -------


def test_singleton_400_backoff_tracks_configured_cap(settings):
    # Checkpoint corrective (finding #5): the singleton-400 delay is a
    # fixed multiple (2x) of the configured cap, not the bare cap value
    # -- see _configured_protocol_fault_backoff_seconds() for why the
    # bare-cap version could tie with the ordinary transient path's own
    # worst-case ceiling.
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 500.0
    row = _make_outbox()
    now = timezone.now()

    _release_singleton_protocol_fault(row, now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.next_attempt_at == now + timedelta(seconds=1000)


def test_singleton_400_backoff_is_unaffected_by_base_setting(settings):
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 50.0
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 100.0
    row = _make_outbox()
    now = timezone.now()

    _release_singleton_protocol_fault(row, now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.next_attempt_at == now + timedelta(seconds=200)


def test_singleton_400_backoff_never_collapses_to_ordinary_short_cadence(settings):
    """Even with a tiny configured cap, the singleton-400 path must still
    be the LONG (cap-derived) backoff -- never accidentally reuse
    release_transient()'s ordinary exponential/base-driven schedule."""
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 2.0
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 30.0
    protocol_row = _make_outbox()
    transient_row = _make_outbox(attempts=0)
    now = timezone.now()

    _release_singleton_protocol_fault(protocol_row, now)
    with patch("linuxlab.sync.outbox.random.random", return_value=1.0):
        release_transient([transient_row], "timeout", now=now)

    protocol_next = OutboxMessage.objects.get(pk=protocol_row.pk).next_attempt_at
    transient_next = OutboxMessage.objects.get(pk=transient_row.pk).next_attempt_at
    assert protocol_next == now + timedelta(seconds=60)
    assert protocol_next > transient_next


def test_singleton_400_backoff_stays_strictly_longer_even_at_the_adversarial_jitter_boundary(
    settings,
):
    """Checkpoint corrective (finding #5): base=cap/2 makes the ordinary
    path's ceiling reach `cap` after a single attempt -- with the worst
    possible (mocked) full-jitter draw of 1.0, the OLD bare-cap singleton
    delay would have tied with it exactly. The 2x multiplier must keep
    the singleton delay strictly greater even in this adversarial case.
    """
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 15.0
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 30.0
    protocol_row = _make_outbox()
    transient_row = _make_outbox(attempts=0)
    now = timezone.now()

    _release_singleton_protocol_fault(protocol_row, now)
    with patch("linuxlab.sync.outbox.random.random", return_value=1.0):
        release_transient([transient_row], "timeout", now=now)

    protocol_next = OutboxMessage.objects.get(pk=protocol_row.pk).next_attempt_at
    transient_next = OutboxMessage.objects.get(pk=transient_row.pk).next_attempt_at
    assert transient_next == now + timedelta(seconds=30)  # ceiling reached at attempt 1
    assert protocol_next > transient_next


# --- checkpoint corrective (finding #5): fail closed on pathological values -


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


def test_singleton_protocol_fault_fails_closed_on_pathological_backoff_settings(settings):
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 999.0
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 5.0
    row = _make_outbox()

    with pytest.raises(BackoffConfigurationError):
        _release_singleton_protocol_fault(row, timezone.now())

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.state == OUTBOX_IN_FLIGHT


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
