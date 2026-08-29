"""Deterministic PostgreSQL wait-state polling for this app's concurrency
tests only.

Real observable PostgreSQL backend state -- never sleeps -- is the
synchronization primitive Task 09's concurrency tests rely on to prove
publisher-vs-publisher ordering and MVCC snapshot coherence. Confirming a
concurrent backend is ACTUALLY queued on the publication advisory mutex
(via pg_stat_activity, a shared, always-visible catalog) before releasing
the mutex holder is what makes "publisher B cannot take its snapshot until
publisher A releases the mutex" a proven fact rather than a timing guess;
the bounded poll loop below is a wait-for-a-real-condition, not a blind
delay.
"""

import time

from django.db import connection


def wait_until_n_backends_are_advisory_lock_blocked(n=1, timeout=5.0, interval=0.01):
    """Block until at least `n` OTHER backends on this database are waiting
    on a PostgreSQL advisory lock, or raise if that never happens within
    `timeout` seconds."""
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
            if count >= n:
                return
            time.sleep(interval)
    raise AssertionError(f"fewer than {n} backend(s) became advisory-lock-blocked within {timeout}s")


def wait_until_a_backend_is_advisory_lock_blocked(timeout=5.0, interval=0.01):
    wait_until_n_backends_are_advisory_lock_blocked(n=1, timeout=timeout, interval=interval)


def assert_no_backend_is_row_lock_blocked():
    """Task 09 final correction: publication no longer takes any row lock,
    so nothing should ever show wait_event_type='Lock' with a NON-advisory
    wait_event (e.g. 'relation', 'tuple', 'transactionid') because of it."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pid, wait_event FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND wait_event_type = 'Lock' AND wait_event <> 'advisory'"
        )
        rows = cursor.fetchall()
    assert rows == [], f"unexpected row-lock waiters: {rows}"
