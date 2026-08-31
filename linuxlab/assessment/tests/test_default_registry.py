"""T15-D27 S10: the production QuestionRegistry builder, reachable outside
any pytest fixture."""

from linuxlab.assessment.default_registry import build_default_question_registry
from linuxlab.assessment.providers import SingleChoiceProvider
from linuxlab.assessment.registry import QuestionRegistry


def test_default_question_registry_is_a_question_registry():
    registry = build_default_question_registry()

    assert isinstance(registry, QuestionRegistry)


def test_default_question_registry_contains_single_choice_provider():
    registry = build_default_question_registry()

    provider = registry.get("single_choice")

    assert isinstance(provider, SingleChoiceProvider)


def test_default_question_registry_builds_a_fresh_instance_each_call():
    first = build_default_question_registry()
    second = build_default_question_registry()

    assert first is not second
    assert first.get("single_choice") is not second.get("single_choice")
