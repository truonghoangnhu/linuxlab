"""T15-D27 S10: the one production QuestionRegistry builder, used by
ordinary runtime/service code and the Release E2E alike -- not a pytest
fixture, not a release-gate-only construct."""

from linuxlab.assessment.providers import SingleChoiceProvider
from linuxlab.assessment.registry import QuestionRegistry


def build_default_question_registry():
    registry = QuestionRegistry()
    registry.register(SingleChoiceProvider())
    return registry
