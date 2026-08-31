"""T15-D27 S10: the production ActivityRegistry builder, reachable outside
any pytest fixture. Used by both ordinary runtime/service code and the
Release E2E."""

from linuxlab.activities.default_registry import build_default_activity_registry
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.quiz.provider import AssessmentActivityProvider


def test_default_activity_registry_is_an_activity_registry():
    registry = build_default_activity_registry()

    assert isinstance(registry, ActivityRegistry)


def test_default_activity_registry_contains_assessment_provider():
    registry = build_default_activity_registry()

    provider = registry.get("assessment")

    assert isinstance(provider, AssessmentActivityProvider)


def test_default_activity_registry_assessment_provider_has_a_question_registry():
    registry = build_default_activity_registry()

    provider = registry.get("assessment")

    assert provider.question_registry.get("single_choice") is not None


def test_default_activity_registry_builds_a_fresh_instance_each_call():
    first = build_default_activity_registry()
    second = build_default_activity_registry()

    assert first is not second
    assert first.get("assessment") is not second.get("assessment")
