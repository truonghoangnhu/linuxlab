"""Corrective B Task 5: real PostgreSQL concurrency evidence for the
NodeRegistration lifecycle authority (issue/rotate/revoke/authenticate).

Coherence comes entirely from `lock_registration_scope()`'s
transaction-scoped PostgreSQL advisory lock plus a fresh ACTIVE
resolution after acquiring it -- not from any row lock, and not from
accidental single-threaded test execution. Every test below forces a
genuine two-backend race and, where order matters, forces a
deterministic winner by confirming (via `pg_stat_activity`, never a
timing sleep) that the second backend is truly blocked waiting on the
real advisory lock before the first is allowed to proceed.
"""

import threading
import time
import uuid

import pytest
from django.db import IntegrityError, close_old_connections, connection

import linuxlab.sync.authentication as auth_module
from linuxlab.sync.authentication import (
    NodeAuthenticationError,
    NodeRegistrationError,
    authenticate_node,
    issue_node_registration,
    revoke_node_registration,
    rotate_node_credential,
)
from linuxlab.sync.models import (
    REGISTRATION_ACTIVE,
    REGISTRATION_REVOKED,
    REGISTRATION_SUPERSEDED,
    NodeRegistration,
)

pytestmark = [pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _central_sync_authority(settings):
    """All registration writers check Central's provisioning capability
    themselves; these concurrency tests exercise lifecycle authority
    itself, not profile gating (covered independently in
    test_task14_profiles.py)."""
    settings.LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = True


# --- real PostgreSQL wait-state polling (never a sleep-based guess) --------


def _wait_until_a_backend_is_advisory_lock_blocked(timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() "
                "AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
            )
            (count,) = cursor.fetchone()
            if count >= 1:
                return
            time.sleep(interval)
    raise AssertionError(f"no backend became advisory-lock-blocked within {timeout}s")


def _assert_no_backend_is_advisory_lock_blocked():
    """Fix round M1 cleanup check: after every race below joins both
    worker threads, no backend may still be waiting on (or, by
    implication, holding open a transaction around) the registration
    advisory lock -- proving both workers actually committed/rolled back
    and released it, not merely that the test's own assertions happened
    to pass."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() "
            "AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
        )
        (count,) = cursor.fetchone()
    assert count == 0, f"{count} backend(s) still advisory-lock-blocked after the race joined"


class _OrderedLockRace:
    """Forces a deterministic winner for two concurrent registration
    writers on the same logical node: `first` genuinely acquires
    `lock_registration_scope`'s real PostgreSQL advisory lock and holds
    the surrounding transaction open -- confirmed via real backend state
    that `second` is genuinely blocked waiting on that same lock -- before
    `first` is allowed to finish and commit."""

    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch
        self._started = threading.Event()
        self._proceed = threading.Event()

    def _install(self):
        real_lock = auth_module.lock_registration_scope

        def _delayed_lock(node_id):
            real_lock(node_id)
            self._started.set()
            self._proceed.wait(timeout=10)

        self._monkeypatch.setattr(auth_module, "lock_registration_scope", _delayed_lock)

    def run(self, first_action, second_action):
        self._install()
        results = {}

        def _run(key, action):
            close_old_connections()
            try:
                results[key] = action()
            except Exception as exc:  # noqa: BLE001
                results[key] = exc
            finally:
                connection.close()

        t_first = threading.Thread(target=_run, args=("first", first_action))
        t_second = threading.Thread(target=_run, args=("second", second_action))

        t_first.start()
        assert self._started.wait(timeout=10), "first worker never reached the real lock"
        t_second.start()
        _wait_until_a_backend_is_advisory_lock_blocked()
        self._proceed.set()
        t_first.join(timeout=10)
        t_second.join(timeout=10)
        assert not t_first.is_alive(), "first worker thread never finished"
        assert not t_second.is_alive(), "second worker thread never finished"
        # Cleanup/order proof (fix round M1): by construction `second`'s
        # real lock acquisition cannot return until `first`'s transaction
        # has committed or rolled back and released the advisory lock, so
        # every effect `second` observes already reflects `first`'s
        # outcome -- and once both threads have joined, nothing may still
        # be holding or waiting on the lock.
        _assert_no_backend_is_advisory_lock_blocked()
        return results


def _is_error(value, error_type=NodeRegistrationError):
    return isinstance(value, error_type)


# --- two concurrent rotations of the same node -----------------------------


def test_two_concurrent_rotations_both_succeed_and_only_the_last_committed_secret_authenticates(
    monkeypatch,
):
    credential0 = issue_node_registration("Lab Serial")
    node_id = credential0.node_id

    race = _OrderedLockRace(monkeypatch)
    results = race.run(
        lambda: rotate_node_credential(node_id),
        lambda: rotate_node_credential(node_id),
    )

    first_result = results["first"]
    second_result = results["second"]
    assert not isinstance(first_result, Exception), first_result
    assert not isinstance(second_result, Exception), second_result

    rows = list(NodeRegistration.objects.filter(node_id=node_id).order_by("pk"))
    assert len(rows) == 3
    active_rows = [row for row in rows if row.state == REGISTRATION_ACTIVE]
    superseded_rows = [row for row in rows if row.state == REGISTRATION_SUPERSEDED]
    assert len(active_rows) == 1
    assert len(superseded_rows) == 2
    # Order proof: the surviving ACTIVE row must be the temporally-last
    # insert (highest pk) -- the second rotation genuinely committed
    # after the first, never the reverse and never a torn interleaving.
    assert active_rows[0].pk == max(row.pk for row in rows)

    with pytest.raises(NodeAuthenticationError):
        authenticate_node(node_id, credential0.secret)

    outcomes = []
    for secret in (first_result.secret, second_result.secret):
        try:
            authenticate_node(node_id, secret)
            outcomes.append(True)
        except NodeAuthenticationError:
            outcomes.append(False)
    assert sorted(outcomes) == [False, True]


# --- rotate vs. revoke race, forced both orderings --------------------------


def test_rotate_then_revoke_yields_zero_active(monkeypatch):
    credential = issue_node_registration("Lab RaceA")
    node_id = credential.node_id

    race = _OrderedLockRace(monkeypatch)
    results = race.run(
        lambda: rotate_node_credential(node_id),
        lambda: revoke_node_registration(node_id),
    )

    rotated = results["first"]
    assert not isinstance(rotated, Exception), rotated
    assert results["second"] is None

    rows = list(NodeRegistration.objects.filter(node_id=node_id).order_by("pk"))
    assert len(rows) == 2
    original, rotated_row = rows
    assert original.state == REGISTRATION_SUPERSEDED
    # Order proof: revoke must have acted on the row rotate just
    # inserted (never the original, which rotate already superseded
    # before revoke's lock call could even return) -- i.e. revoke's
    # fresh ACTIVE resolution genuinely observed rotate's committed
    # state, not a stale pre-rotation read.
    assert rotated_row.node_id == node_id
    assert rotated_row.state == REGISTRATION_REVOKED
    assert rotated_row.revoked_at is not None
    assert rotated_row.rotated_at is None

    assert (
        NodeRegistration.objects.filter(node_id=node_id, state=REGISTRATION_ACTIVE).count() == 0
    )


def test_revoke_then_rotate_makes_rotate_fail(monkeypatch):
    credential = issue_node_registration("Lab RaceB")
    node_id = credential.node_id

    race = _OrderedLockRace(monkeypatch)
    results = race.run(
        lambda: revoke_node_registration(node_id),
        lambda: rotate_node_credential(node_id),
    )

    assert results["first"] is None
    assert _is_error(results["second"])

    # Order proof: rotate's fresh ACTIVE resolution genuinely observed
    # revoke's already-committed REVOKED state (zero ACTIVE), never a
    # stale pre-revoke read that would have let it rotate anyway.
    rows = list(NodeRegistration.objects.filter(node_id=node_id))
    assert len(rows) == 1
    assert rows[0].state == REGISTRATION_REVOKED
    assert rows[0].revoked_at is not None

    assert (
        NodeRegistration.objects.filter(node_id=node_id, state=REGISTRATION_ACTIVE).count() == 0
    )


# --- explicit issue against an existing ACTIVE scope, forced both orderings


def test_issue_against_active_fails_when_issue_wins_the_lock_first(monkeypatch):
    credential = issue_node_registration("Lab RaceC")
    node_id = credential.node_id

    race = _OrderedLockRace(monkeypatch)
    results = race.run(
        lambda: issue_node_registration("Lab RaceC replacement", node_id=node_id),
        lambda: rotate_node_credential(node_id),
    )

    assert _is_error(results["first"])
    rotated = results["second"]
    assert not isinstance(rotated, Exception), rotated

    # Order proof: issue's own ACTIVE check ran first, against the
    # original (pre-rotation) row, and rejected immediately -- it never
    # left a stray row behind, and the rotation that followed still
    # completed normally against the still-existing original ACTIVE.
    rows = list(NodeRegistration.objects.filter(node_id=node_id).order_by("pk"))
    assert len(rows) == 2
    original, replacement = rows
    assert original.state == REGISTRATION_SUPERSEDED
    assert replacement.state == REGISTRATION_ACTIVE
    authenticate_node(node_id, rotated.secret)


def test_issue_against_active_fails_when_rotate_wins_the_lock_first(monkeypatch):
    credential = issue_node_registration("Lab RaceD")
    node_id = credential.node_id

    race = _OrderedLockRace(monkeypatch)
    results = race.run(
        lambda: rotate_node_credential(node_id),
        lambda: issue_node_registration("Lab RaceD replacement", node_id=node_id),
    )

    rotated = results["first"]
    assert not isinstance(rotated, Exception), rotated
    assert _is_error(results["second"])

    # Order proof: rotate committed first, so by the time issue's own
    # ACTIVE check ran it observed the ROTATED row (never the original,
    # already superseded) and still correctly rejected -- issue never
    # created a stray row on top of the rotation.
    rows = list(NodeRegistration.objects.filter(node_id=node_id).order_by("pk"))
    assert len(rows) == 2
    original, replacement = rows
    assert original.state == REGISTRATION_SUPERSEDED
    assert replacement.state == REGISTRATION_ACTIVE
    authenticate_node(node_id, rotated.secret)


# --- explicit issue racing itself in a zero-ACTIVE scope --------------------


def test_explicit_issue_in_zero_active_scope_races_safely(monkeypatch):
    node_id = uuid.uuid4()

    race = _OrderedLockRace(monkeypatch)
    results = race.run(
        lambda: issue_node_registration("Lab RaceE first", node_id=node_id),
        lambda: issue_node_registration("Lab RaceE second", node_id=node_id),
    )

    assert not isinstance(results["first"], Exception), results["first"]
    assert _is_error(results["second"])

    # Order proof: exactly the row "first" inserted survives -- "second"
    # observed it already committed and rejected, never raced to insert
    # its own row too.
    rows = NodeRegistration.objects.filter(node_id=node_id)
    assert rows.count() == 1
    survivor = rows.get()
    assert survivor.state == REGISTRATION_ACTIVE
    assert survivor.display_name == "Lab RaceE first"
    authenticate_node(node_id, results["first"].secret)


# --- insertion failure rolls back the supersede (single-threaded) ---------


def test_rotate_insertion_failure_rolls_back_the_supersede(monkeypatch):
    credential = issue_node_registration("Lab Rollback")

    def _boom(model_cls, **fields):
        del model_cls, fields
        raise IntegrityError("simulated insert failure")

    monkeypatch.setattr(auth_module, "_insert_immutable_row", _boom)

    with pytest.raises(IntegrityError):
        rotate_node_credential(credential.node_id)

    registration = NodeRegistration.objects.get(node_id=credential.node_id)
    assert registration.state == REGISTRATION_ACTIVE
    authenticate_node(credential.node_id, credential.secret)
