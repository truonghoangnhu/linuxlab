"""Task 15 Tasks 7-10 final cleanup corrective: cleanup-action
aggregation and deployment-owned workspace removal.

run_cleanup_actions() attempts every registered cleanup action exactly
once, regardless of whether an earlier one failed -- the hard
invariant that Node database cleanup failing must never skip Central
database cleanup (and vice versa) generalizes to every registered
cleanup step, not just the two database drops. Only after every action
has been attempted does it raise a single aggregated error naming each
one that failed. It never silently swallows a security-relevant
cleanup failure (a leaked private key, an undropped database) by
catching and discarding it.

If a caller's own scenario body was already propagating an exception
when cleanup ran (the normal `finally`-block case), Python's own
implicit exception chaining (`__context__`) preserves that original
exception automatically once CleanupAggregateError is raised from
inside that `finally` -- no special handling is needed here to keep it
visible to whoever ultimately observes the traceback.
"""

import shutil
from pathlib import Path


class CleanupAggregateError(Exception):
    """Raised by run_cleanup_actions() after every registered action has
    been attempted, naming each one that failed. `failures` is a list of
    (name, exception) pairs, in the order those actions were attempted."""

    def __init__(self, failures):
        self.failures = list(failures)
        summary = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in self.failures)
        super().__init__(f"{len(self.failures)} cleanup action(s) failed: {summary}")


def run_cleanup_actions(actions) -> None:
    """`actions`: an iterable of (name, callable) pairs. Every callable
    is invoked exactly once, in order, regardless of whether an earlier
    one raised. Raises CleanupAggregateError iff at least one action
    failed, only after every action has been attempted."""
    failures = []
    for name, action in actions:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 -- collected, never silently dropped
            failures.append((name, exc))
    if failures:
        raise CleanupAggregateError(failures)


def remove_workspace(path) -> None:
    """Recursively removes a deployment-owned artifact workspace
    directory -- every generated release artifact (private/public keys,
    .llpkg/tampered .llpkg, pg_dump files, Gunicorn logs, other
    scenario-generated temp artifacts) lives inside it, so removing the
    directory removes all of them in one step. Idempotent: a safe no-op
    if the workspace was never created or has already been removed.
    Never touches the parent directory or any sibling path -- only the
    exact workspace path given."""
    try:
        shutil.rmtree(Path(path))
    except FileNotFoundError:
        pass
