from typing import Any, Protocol


class ActivityProvider(Protocol):
    """Stable Phase 1 extension boundary for interactive learning (ADR-004, ADR-009).

    Learning communicates with providers only through this contract; provider
    internals must not leak into Lesson/Learning models.
    """

    type_key: str

    def validate_config(self, config: dict[str, Any]) -> None: ...

    def evaluate(self, config: dict[str, Any], submission: Any) -> dict[str, Any]: ...
