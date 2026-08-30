"""Task 13 server-only grading runtime service: get_runtime_question_grading()
lives in its own module (linuxlab.runtime.grading), is never exported by
the learner-facing linuxlab.runtime facade, and returns a deep-immutable
RuntimeQuestionGrading -- never merged into RuntimeQuestion (T13-D06,
T13-D07)."""

import pytest
from django.test import override_settings

from linuxlab.runtime.context import open_runtime_context
from linuxlab.runtime.dto import RuntimeQuestionGrading
from linuxlab.runtime.exceptions import RuntimeArtifactNotFound
from linuxlab.runtime.grading import get_runtime_question_grading
from linuxlab.sync.models import InstalledQuestionVersion
from linuxlab.sync.services import activate_package

from .conftest import ANSWER_KEY, SCORING, build_quiz_world, stage

pytestmark = pytest.mark.django_db(transaction=True)


def _context(world):
    package = world["package"]
    return open_runtime_context(package.package_type, package.source_public_id)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_grading_is_available_through_the_grading_service(installed_active_world):
    world = installed_active_world
    ctx = _context(world)
    installed_question = InstalledQuestionVersion.objects.get(package=world["package"])

    grading = get_runtime_question_grading(
        ctx, installed_question.question_public_id, installed_question.version
    )

    assert isinstance(grading, RuntimeQuestionGrading)
    assert grading.question_public_id == installed_question.question_public_id
    assert grading.version == installed_question.version
    assert dict(grading.answer_key) == ANSWER_KEY
    assert dict(grading.scoring) == SCORING


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_grading_is_deeply_immutable(installed_active_world):
    world = installed_active_world
    ctx = _context(world)
    installed_question = InstalledQuestionVersion.objects.get(package=world["package"])

    grading = get_runtime_question_grading(
        ctx, installed_question.question_public_id, installed_question.version
    )

    with pytest.raises(TypeError):
        grading.answer_key["correct_option_id"] = "other"
    with pytest.raises(TypeError):
        grading.scoring["max_score"] = 999


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_grading_missing_question_raises_not_found(installed_active_world):
    import uuid

    world = installed_active_world
    ctx = _context(world)

    with pytest.raises(RuntimeArtifactNotFound):
        get_runtime_question_grading(ctx, uuid.uuid4(), 1)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_grading_from_a_different_package_is_not_found(installed_active_world):
    other_world = build_quiz_world()
    private_key, public_key = installed_active_world["private_key"], installed_active_world[
        "public_key"
    ]
    other_package = stage(other_world, private_key, public_key)
    activate_package(other_package)
    other_question = InstalledQuestionVersion.objects.get(package=other_package)

    ctx = _context(installed_active_world)

    with pytest.raises(RuntimeArtifactNotFound):
        get_runtime_question_grading(ctx, other_question.question_public_id, other_question.version)


def test_runtime_facade_does_not_export_grading_lookup():
    from linuxlab import runtime

    assert not hasattr(runtime, "get_runtime_question_grading")
    assert not hasattr(runtime, "RuntimeQuestionGrading")
