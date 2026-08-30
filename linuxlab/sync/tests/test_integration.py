"""Required Task 12 core integration scenario (§58): a REAL package built
from LearningPath -> Assessment ActivityVersion -> QuestionVersion ->
PublicationSnapshot -> build_package(), staged through the public
import_package() service, then taken through the full
stage -> activate -> stage-newer -> activate-newer -> rollback lifecycle,
asserting every persisted artifact model and every lifecycle transition
along the way. No mocks for the core path; no Central/Internet access at
any lifecycle step."""

import json

import pytest
from django.test import override_settings

from linuxlab.publication.package import build_package
from linuxlab.sync.models import (
    ACTIVE,
    ARCHIVED,
    STAGED,
    InstalledActivityVersion,
    InstalledPackage,
    InstalledPublication,
    InstalledQuestionGrading,
    InstalledQuestionVersion,
)
from linuxlab.sync.services import activate_package, import_package, rollback_package

from .conftest import ANSWER_KEY, CONTENT, SCORING, republish

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_full_verify_stage_activate_stage_activate_rollback_lifecycle(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    # --- 1. build + stage the first real, signed package ------------------
    first_bytes = build_package(quiz_world["snapshot"], registry, private_key)
    first = import_package(first_bytes, registry, public_key)

    assert isinstance(first, InstalledPackage)
    assert first.state == STAGED
    assert first.package_type == "learning_path"
    assert first.source_public_id == quiz_world["snapshot"].source_public_id
    assert first.package_version == quiz_world["snapshot"].version

    installed_publication = InstalledPublication.objects.get(package=first)
    assert installed_publication.content_hash == quiz_world["snapshot"].content_hash
    assert installed_publication.payload == quiz_world["snapshot"].payload

    installed_activity = InstalledActivityVersion.objects.get(package=first)
    assert installed_activity.activity_public_id == quiz_world["definition"].public_id
    assert installed_activity.version == quiz_world["activity_version"].version
    assert installed_activity.type_key == "assessment"

    installed_question = InstalledQuestionVersion.objects.get(package=first)
    assert installed_question.question_public_id == quiz_world["question"].public_id
    assert installed_question.content == CONTENT

    installed_grading = InstalledQuestionGrading.objects.get(question=installed_question)
    assert installed_grading.answer_key == ANSWER_KEY
    assert installed_grading.scoring == SCORING

    # Learner-safe artifacts never carry grading secrets.
    learner_serialized = json.dumps(
        {
            "publication": installed_publication.payload,
            "activities": [installed_activity.config],
            "questions": [installed_question.content],
        }
    )
    assert "answer_key" not in learner_serialized
    assert "correct_option_id" not in learner_serialized

    # --- 2. activate the first package --------------------------------------
    activate_package(first)
    first.refresh_from_db()
    assert first.state == ACTIVE
    assert first.previous_package is None

    # --- 3. build + stage a newer version; must NOT auto-activate ----------
    second_snapshot = republish(quiz_world)
    second_bytes = build_package(second_snapshot, registry, private_key)
    second = import_package(second_bytes, registry, public_key)

    first.refresh_from_db()
    assert first.state == ACTIVE
    assert second.state == STAGED

    # --- 4. activate the newer version --------------------------------------
    activate_package(second)
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.state == ARCHIVED
    assert second.state == ACTIVE
    assert second.previous_package_id == first.pk

    # --- 5. rollback to the first version -----------------------------------
    rollback_package(second.package_type, second.source_public_id)
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.state == ACTIVE
    assert second.state == ARCHIVED

    # Rollback reused the original staged artifacts verbatim.
    assert InstalledPublication.objects.get(package=first).payload == quiz_world["snapshot"].payload
    assert InstalledActivityVersion.objects.filter(package=first).count() == 1
    assert InstalledQuestionVersion.objects.filter(package=first).count() == 1
    assert InstalledQuestionGrading.objects.filter(question__package=first).count() == 1
