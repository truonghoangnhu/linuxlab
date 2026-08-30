"""Task 13 runtime DTOs and deep-freeze utility: frozen dataclasses alone
only protect field assignment, not nested dict/list contents -- every
JSON-bearing field must pass through freeze_json_value() first."""

from dataclasses import FrozenInstanceError
from types import MappingProxyType
from uuid import uuid4

import pytest

from linuxlab.runtime.dto import (
    RuntimeActivity,
    RuntimePackageContext,
    RuntimePublication,
    RuntimeQuestion,
    RuntimeQuestionGrading,
)
from linuxlab.runtime.freeze import freeze_json_value


def test_freeze_json_value_recursively_freezes_mapping_and_lists():
    frozen = freeze_json_value({"items": [{"name": "x", "flags": [True, False]}]})

    assert isinstance(frozen, MappingProxyType)
    assert isinstance(frozen["items"], tuple)
    assert isinstance(frozen["items"][0], MappingProxyType)
    assert frozen["items"][0]["flags"] == (True, False)

    with pytest.raises(TypeError):
        frozen["new"] = "value"


def test_freeze_json_value_leaves_scalars_unchanged():
    assert freeze_json_value("x") == "x"
    assert freeze_json_value(1) == 1
    assert freeze_json_value(1.5) == 1.5
    assert freeze_json_value(True) is True
    assert freeze_json_value(None) is None


def test_freeze_json_value_freezes_empty_containers():
    assert freeze_json_value({}) == MappingProxyType({})
    assert freeze_json_value([]) == ()


def test_freeze_json_value_rejects_unsupported_types():
    class NotJson:
        pass

    with pytest.raises(TypeError):
        freeze_json_value(NotJson())


def test_freeze_json_value_produces_a_detached_copy():
    original = {"a": [1, 2]}
    frozen = freeze_json_value(original)

    original["a"].append(3)
    original["b"] = "new"

    assert frozen["a"] == (1, 2)
    assert "b" not in frozen


def test_runtime_activity_is_frozen_and_nested_config_is_immutable():
    activity = RuntimeActivity(
        activity_public_id=uuid4(),
        version=1,
        type_key="assessment",
        config=freeze_json_value({"questions": [{"version": 1}]}),
    )

    with pytest.raises(FrozenInstanceError):
        activity.version = 2

    with pytest.raises(TypeError):
        activity.config["questions"] = ()


def test_runtime_package_context_is_frozen():
    context = RuntimePackageContext(
        package_pk=1,
        package_type="learning_path",
        source_public_id=uuid4(),
        package_version=1,
        payload_sha256="0" * 64,
        _provenance_tag=b"0" * 32,
    )

    with pytest.raises(FrozenInstanceError):
        context.package_pk = 2


def test_runtime_package_context_provenance_tag_is_excluded_from_repr():
    context = RuntimePackageContext(
        package_pk=1,
        package_type="learning_path",
        source_public_id=uuid4(),
        package_version=1,
        payload_sha256="0" * 64,
        _provenance_tag=b"super-secret-tag-bytes",
    )

    assert "super-secret-tag-bytes" not in repr(context)
    assert "_provenance_tag" not in repr(context)


def test_runtime_publication_is_frozen_with_immutable_payload():
    publication = RuntimePublication(
        kind="learning_path",
        source_public_id=uuid4(),
        version=1,
        content_hash="0" * 64,
        payload=freeze_json_value({"learning_path": {"modules": []}}),
    )

    with pytest.raises(FrozenInstanceError):
        publication.version = 2
    with pytest.raises(TypeError):
        publication.payload["tampered"] = True


def test_runtime_question_has_no_grading_fields():
    question = RuntimeQuestion(
        question_public_id=uuid4(),
        version=1,
        type_key="single_choice",
        content=freeze_json_value({"prompt": "?"}),
    )

    assert not hasattr(question, "answer_key")
    assert not hasattr(question, "scoring")


def test_runtime_question_grading_is_frozen_with_immutable_json():
    grading = RuntimeQuestionGrading(
        question_public_id=uuid4(),
        version=1,
        answer_key=freeze_json_value({"correct_option_id": "a"}),
        scoring=freeze_json_value({"max_score": 1}),
    )

    with pytest.raises(FrozenInstanceError):
        grading.version = 2
    with pytest.raises(TypeError):
        grading.answer_key["correct_option_id"] = "z"
