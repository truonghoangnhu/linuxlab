import uuid
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ActivityDependencies:
    """The exact immutable artifacts one ActivityVersion's config depends
    on (Task 11: package dependency closure). Every reference is an exact
    (public_id, version) pair -- never "latest" -- matching the same
    exact-version convention used everywhere else in this codebase.

    Silence is not a valid dependency declaration (Task 11 final
    correction): a provider with no external dependencies must call
    ActivityDependencies.empty() explicitly, rather than package/build code
    inferring "none" because collect_dependencies() happens to be missing.
    """

    question_versions: tuple[tuple[uuid.UUID, int], ...] = ()

    @classmethod
    def empty(cls) -> "ActivityDependencies":
        return cls()


class ActivityProvider(Protocol):
    """Stable Phase 1 extension boundary for interactive learning (ADR-004, ADR-009).

    Learning communicates with providers only through this contract; provider
    internals must not leak into Lesson/Learning models.
    """

    type_key: str

    def validate_config(self, config: dict[str, Any]) -> None: ...

    def evaluate(self, config: dict[str, Any], submission: Any) -> dict[str, Any]: ...

    def collect_dependencies(self, config: dict[str, Any]) -> ActivityDependencies:
        """Required (Task 11 final correction): return the exact immutable
        artifacts this config depends on, for package dependency closure. A
        provider with no dependencies must return ActivityDependencies.empty()
        explicitly. Package build/verify code treats a provider that omits
        this method as a fail-closed error, never as "no dependencies" --
        an accidentally-incomplete provider must not be able to produce a
        package Task 12 later discovers is missing data it needed.
        """
        ...
