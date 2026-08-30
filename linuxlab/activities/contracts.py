import uuid
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ActivityDependencies:
    """The exact immutable artifacts one ActivityVersion's config depends
    on (Task 11: package dependency closure). Every reference is an exact
    (public_id, version) pair -- never "latest" -- matching the same
    exact-version convention used everywhere else in this codebase.

    A provider with no dependencies -- the default for any provider that
    does not implement collect_dependencies() at all -- is
    ActivityDependencies(), i.e. every field empty.
    """

    question_versions: tuple[tuple[uuid.UUID, int], ...] = ()


class ActivityProvider(Protocol):
    """Stable Phase 1 extension boundary for interactive learning (ADR-004, ADR-009).

    Learning communicates with providers only through this contract; provider
    internals must not leak into Lesson/Learning models.
    """

    type_key: str

    def validate_config(self, config: dict[str, Any]) -> None: ...

    def evaluate(self, config: dict[str, Any], submission: Any) -> dict[str, Any]: ...

    def collect_dependencies(self, config: dict[str, Any]) -> ActivityDependencies:
        """Optional (Task 11): return the exact immutable artifacts this
        config depends on, for package dependency closure. A provider that
        does not implement this method is treated by callers as having no
        dependencies -- this method exists on the Protocol as documentation
        of the extension point, not as a requirement every existing
        provider must implement.
        """
        ...
