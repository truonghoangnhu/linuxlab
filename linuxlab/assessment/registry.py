from linuxlab.assessment.contracts import QuestionProvider


class QuestionRegistry:
    """Explicit in-memory provider lookup. No auto-discovery, no global state."""

    def __init__(self):
        self._providers: dict[str, QuestionProvider] = {}

    def register(self, provider: QuestionProvider) -> None:
        if provider.type_key in self._providers:
            raise ValueError(f"a provider is already registered for type_key '{provider.type_key}'")
        self._providers[provider.type_key] = provider

    def get(self, type_key: str) -> QuestionProvider:
        try:
            return self._providers[type_key]
        except KeyError:
            raise LookupError(f"no provider registered for type_key '{type_key}'") from None
