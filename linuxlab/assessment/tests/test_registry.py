import pytest

from linuxlab.assessment.registry import QuestionRegistry


class FakeQuestionProvider:
    type_key = "fake"

    def validate(self, content, answer_key):
        if "expected" not in answer_key:
            raise ValueError("answer_key must contain 'expected'")

    def learner_payload(self, content):
        return {"type": self.type_key, "prompt": content.get("prompt")}

    def evaluate(self, content, answer_key, response):
        correct = response == answer_key["expected"]
        return {"correct": correct, "score": 1 if correct else 0, "max_score": 1}


def test_provider_registers_and_can_be_looked_up():
    registry = QuestionRegistry()
    provider = FakeQuestionProvider()

    registry.register(provider)

    assert registry.get("fake") is provider


def test_duplicate_type_key_registration_is_rejected():
    registry = QuestionRegistry()
    registry.register(FakeQuestionProvider())

    with pytest.raises(ValueError):
        registry.register(FakeQuestionProvider())


def test_unknown_type_key_lookup_is_rejected():
    registry = QuestionRegistry()

    with pytest.raises(LookupError):
        registry.get("does-not-exist")


def test_registry_instances_are_isolated():
    first = QuestionRegistry()
    second = QuestionRegistry()

    first.register(FakeQuestionProvider())

    with pytest.raises(LookupError):
        second.get("fake")
