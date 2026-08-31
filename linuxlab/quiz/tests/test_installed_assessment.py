"""T15-D27 S8, S11: AssessmentActivityProvider.evaluate(context=...) and
serialize_installed_assessment_for_learner() resolve from a pinned
InstalledPackage's InstalledQuestionVersion/InstalledQuestionGrading --
never the live authoring QuestionVersion table."""

import pytest
from django.test import override_settings

from linuxlab.quiz.provider import serialize_installed_assessment_for_learner
from linuxlab.runtime.content import get_runtime_activity
from linuxlab.runtime.context import open_runtime_context

pytestmark = pytest.mark.django_db(transaction=True)


def _context(world):
    package = world["package"]
    return open_runtime_context(package.package_type, package.source_public_id)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_evaluate_with_context_grades_from_installed_artifacts(installed_active_world):
    world = installed_active_world
    context = _context(world)
    activity = get_runtime_activity(context, world["definition"].public_id, world["activity_version"].version)
    provider = world["activity_registry"].get("assessment")

    outcome = provider.evaluate(
        activity.config,
        {
            "responses": [
                {
                    "question_public_id": str(world["question"].public_id),
                    "question_version": world["question_version"].version,
                    "selected_option_id": "a",
                }
            ]
        },
        context=context,
    )

    assert outcome["score"] == 1
    assert outcome["max_score"] == 1


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_evaluate_without_context_still_resolves_from_central_table(installed_active_world):
    """context=None (the default) is unchanged Central behavior -- proven
    here using the SAME published ActivityVersion the installed world was
    built from, confirming the two resolution paths agree."""
    world = installed_active_world
    provider = world["activity_registry"].get("assessment")

    outcome = provider.evaluate(
        world["activity_version"].config,
        {
            "responses": [
                {
                    "question_public_id": str(world["question"].public_id),
                    "question_version": world["question_version"].version,
                    "selected_option_id": "a",
                }
            ]
        },
    )

    assert outcome["score"] == 1
    assert outcome["max_score"] == 1


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_serialize_installed_assessment_for_learner_matches_central_shape(installed_active_world):
    world = installed_active_world
    context = _context(world)
    activity = get_runtime_activity(context, world["definition"].public_id, world["activity_version"].version)
    provider = world["activity_registry"].get("assessment")

    payload = serialize_installed_assessment_for_learner(context, activity, provider.question_registry)

    assert payload["activity_public_id"] == str(world["definition"].public_id)
    assert payload["activity_version"] == world["activity_version"].version
    assert len(payload["questions"]) == 1
    question_payload = payload["questions"][0]
    assert question_payload["question_public_id"] == str(world["question"].public_id)
    assert question_payload["prompt"] == "Which command lists files?"
    assert {opt["id"] for opt in question_payload["options"]} == {"a", "b"}


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_serialize_installed_assessment_for_learner_has_no_answer_key_material(installed_active_world):
    import json

    world = installed_active_world
    context = _context(world)
    activity = get_runtime_activity(context, world["definition"].public_id, world["activity_version"].version)
    provider = world["activity_registry"].get("assessment")

    payload = serialize_installed_assessment_for_learner(context, activity, provider.question_registry)
    serialized = json.dumps(payload)

    assert "answer_key" not in serialized
    assert "correct_option_id" not in serialized
    assert "scoring" not in serialized


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_serialize_installed_assessment_for_learner_rejects_non_assessment_activity(
    installed_active_world,
):
    from dataclasses import replace

    world = installed_active_world
    context = _context(world)
    activity = get_runtime_activity(context, world["definition"].public_id, world["activity_version"].version)
    provider = world["activity_registry"].get("assessment")
    not_assessment = replace(activity, type_key="terminal")

    with pytest.raises(ValueError):
        serialize_installed_assessment_for_learner(context, not_assessment, provider.question_registry)
