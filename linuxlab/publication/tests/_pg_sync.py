"""Deterministic PostgreSQL lock-wait polling for this app's concurrency
tests only.

Real Postgres row-lock blocking -- not sleeps -- is the synchronization
primitive Task 09's corrective concurrency tests rely on to prove one-state
capture and publication chronology. Confirming a concurrent backend is
ACTUALLY queued behind a lock (via pg_stat_activity, a shared, always-
visible catalog) before releasing the lock holder is what makes "thread B's
write cannot proceed until thread A releases its lock" a proven fact rather
than a timing guess; the bounded poll loop below is a wait-for-a-real-
condition, not a blind delay.
"""

import time

from django.db import connection


def wait_until_n_backends_are_lock_blocked(n=1, timeout=5.0, interval=0.01):
    """Block until at least `n` OTHER backends on this database are waiting
    on a lock, or raise if that never happens within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
            )
            (count,) = cursor.fetchone()
            if count >= n:
                return
            time.sleep(interval)
    raise AssertionError(f"fewer than {n} backend(s) became lock-blocked within {timeout}s")


def wait_until_a_backend_is_lock_blocked(timeout=5.0, interval=0.01):
    wait_until_n_backends_are_lock_blocked(n=1, timeout=timeout, interval=interval)
