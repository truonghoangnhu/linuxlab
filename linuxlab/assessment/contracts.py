from typing import Any, Protocol


class QuestionProvider(Protocol):
    """Stable extension boundary for question types (ADR-013).

    learner_payload() receives only learner-visible content, never the
    answer_key, so a safe serializer cannot accidentally leak grading data.
    """

    type_key: str

    def validate(self, content: dict[str, Any], answer_key: dict[str, Any]) -> None: ...

    def learner_payload(self, content: dict[str, Any]) -> dict[str, Any]: ...

    def evaluate(
        self, content: dict[str, Any], answer_key: dict[str, Any], response: Any
    ) -> dict[str, Any]: ...
