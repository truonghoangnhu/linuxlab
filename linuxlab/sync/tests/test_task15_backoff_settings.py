"""Task 15: wire LINUXLAB_SYNC_BACKOFF_BASE_SECONDS/CAP_SECONDS into
ordinary transient retry scheduling (closing the accepted Task 14
Minor: these settings existed but release_transient() used hardcoded
module constants instead). T14-D41's singleton-400 long backoff stays
intentionally derived from the same (now-live) cap setting, exactly as
originally designed ("reuses the existing transient-backoff cap") --
it must track the cap when configured, but must never collapse to the
ordinary short/base-driven cadence.
"""

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
from linuxlab.sync.outbox import _release_singleton_protocol_fault, release_transient

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
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 500.0
    row = _make_outbox()
    now = timezone.now()

    _release_singleton_protocol_fault(row, now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.next_attempt_at == now + timedelta(seconds=500)


def test_singleton_400_backoff_is_unaffected_by_base_setting(settings):
    settings.LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = 999.0
    settings.LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = 100.0
    row = _make_outbox()
    now = timezone.now()

    _release_singleton_protocol_fault(row, now)

    refreshed = OutboxMessage.objects.get(pk=row.pk)
    assert refreshed.next_attempt_at == now + timedelta(seconds=100)


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
    assert protocol_next == now + timedelta(seconds=30)
    assert protocol_next > transient_next
