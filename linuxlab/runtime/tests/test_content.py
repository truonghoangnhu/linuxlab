"""Task 13 learner-safe runtime content services: get_runtime_publication/
get_runtime_activity/get_runtime_question. Every lookup rebinds the
context, scopes strictly to the exact pinned InstalledPackage, uses exact
UUID+version, freezes nested JSON, and never leaks a Django model or
grading data (T13-D03, T13-D06)."""

import uuid
from types import MappingProxyType

import pytest
from django.test import override_settings

from linuxlab.publication.package import build_package
from linuxlab.runtime.content import (
    get_runtime_activity,
    get_runtime_publication,
    get_runtime_question,
)
from linuxlab.runtime.context import open_runtime_context
from linuxlab.runtime.dto import RuntimeActivity, RuntimePublication, RuntimeQuestion
from linuxlab.runtime.exceptions import RuntimeArtifactNotFound
from linuxlab.runtime.freeze import freeze_json_value
from linuxlab.sync.models import (
    InstalledActivityVersion,
    InstalledPublication,
    InstalledQuestionVersion,
)
from linuxlab.sync.services import activate_package, import_package

from .conftest import CONTENT, build_quiz_world, republish, stage

pytestmark = pytest.mark.django_db(transaction=True)


def _context(world):
    package = world["package"]
    return open_runtime_context(package.package_type, package.source_public_id)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_publication_returns_dto_not_django_model(installed_active_world):
    world = installed_active_world
    ctx = _context(world)

    publication = get_runtime_publication(ctx)

    assert isinstance(publication, RuntimePublication)
    assert not isinstance(publication, InstalledPublication)
    assert publication.kind == "learning_path"
    assert publication.source_public_id == world["package"].source_public_id
    assert publication.payload == freeze_json_value(world["snapshot"].payload)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_publication_payload_is_deeply_immutable(installed_active_world):
    world = installed_active_world
    ctx = _context(world)

    publication = get_runtime_publication(ctx)

    assert isinstance(publication.payload, MappingProxyType)
    with pytest.raises(TypeError):
        publication.payload["tampered"] = True


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activity_lookup_returns_dto_matching_installed_row(installed_active_world):
    world = installed_active_world
    ctx = _context(world)
    installed = InstalledActivityVersion.objects.get(package=world["package"])

    activity = get_runtime_activity(ctx, installed.activity_public_id, installed.version)

    assert isinstance(activity, RuntimeActivity)
    assert activity.activity_public_id == installed.activity_public_id
    assert activity.version == installed.version
    assert activity.type_key == "assessment"
    assert activity.config == freeze_json_value(installed.config)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activity_lookup_is_scoped_to_the_pinned_package_after_a_new_activation(
    installed_active_world,
):
    world = installed_active_world
    registry = world["activity_registry"]
    private_key, public_key = world["private_key"], world["public_key"]
    v1 = world["package"]
    ctx_v1 = _context(world)
    installed_v1_activity = InstalledActivityVersion.objects.get(package=v1)

    v2_snapshot = republish(world)
    v2 = import_package(build_package(v2_snapshot, registry, private_key), registry, public_key)
    activate_package(v2)

    activity = get_runtime_activity(
        ctx_v1, installed_v1_activity.activity_public_id, installed_v1_activity.version
    )
    assert activity.version == installed_v1_activity.version


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activity_from_a_different_package_is_not_found(installed_active_world):
    other_world = build_quiz_world()
    private_key, public_key = installed_active_world["private_key"], installed_active_world[
        "public_key"
    ]
    other_package = stage(other_world, private_key, public_key)
    activate_package(other_package)
    other_activity = InstalledActivityVersion.objects.get(package=other_package)

    ctx = _context(installed_active_world)

    with pytest.raises(RuntimeArtifactNotFound):
        get_runtime_activity(ctx, other_activity.activity_public_id, other_activity.version)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_activity_missing_exact_uuid_or_version_is_not_found(installed_active_world):
    world = installed_active_world
    ctx = _context(world)
    installed = InstalledActivityVersion.objects.get(package=world["package"])

    with pytest.raises(RuntimeArtifactNotFound):
        get_runtime_activity(ctx, uuid.uuid4(), installed.version)

    with pytest.raises(RuntimeArtifactNotFound):
        get_runtime_activity(ctx, installed.activity_public_id, installed.version + 1)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_question_dto_contains_no_grading_material(installed_active_world):
    world = installed_active_world
    ctx = _context(world)
    installed = InstalledQuestionVersion.objects.get(package=world["package"])

    question = get_runtime_question(ctx, installed.question_public_id, installed.version)

    assert isinstance(question, RuntimeQuestion)
    assert not hasattr(question, "answer_key")
    assert not hasattr(question, "scoring")
    assert question.content == freeze_json_value(CONTENT)
    assert "correct_option_id" not in repr(question.content)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_question_content_is_deeply_immutable(installed_active_world):
    world = installed_active_world
    ctx = _context(world)
    installed = InstalledQuestionVersion.objects.get(package=world["package"])

    question = get_runtime_question(ctx, installed.question_public_id, installed.version)

    with pytest.raises(TypeError):
        question.content["tampered"] = True


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_question_from_a_different_package_is_not_found(installed_active_world):
    other_world = build_quiz_world()
    private_key, public_key = installed_active_world["private_key"], installed_active_world[
        "public_key"
    ]
    other_package = stage(other_world, private_key, public_key)
    activate_package(other_package)
    other_question = InstalledQuestionVersion.objects.get(package=other_package)

    ctx = _context(installed_active_world)

    with pytest.raises(RuntimeArtifactNotFound):
        get_runtime_question(ctx, other_question.question_public_id, other_question.version)
