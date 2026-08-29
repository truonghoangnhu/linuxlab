import uuid

import pytest

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.validators import validate_learning_path

from ._wagtail_bootstrap import get_or_create_root_page


class FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": 1, "max_score": 1}


@pytest.fixture
def registry():
    reg = ActivityRegistry()
    reg.register(FakeProvider())
    return reg


@pytest.fixture
def definition(db):
    return ActivityDefinition.objects.create(name="Fake activity", type_key="fake")


@pytest.fixture
def published_version(definition, registry):
    version = create_activity_version(definition, config={})
    return publish_activity_version(version, registry)


def _root():
    return get_or_create_root_page()


def _make_tree(activity_block_value=None):
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    if activity_block_value is not None:
        lesson.body = [("activity", activity_block_value)]
    module.add_child(instance=lesson)
    return path


@pytest.mark.django_db
def test_learning_path_with_no_activity_references_is_valid():
    path = _make_tree()

    validate_learning_path(path)  # must not raise


@pytest.mark.django_db
def test_valid_published_activity_reference_passes(published_version):
    path = _make_tree(
        {
            "activity_public_id": str(published_version.definition.public_id),
            "activity_version": published_version.version,
        }
    )

    validate_learning_path(path)  # must not raise


@pytest.mark.django_db
def test_missing_activity_definition_is_rejected():
    path = _make_tree({"activity_public_id": str(uuid.uuid4()), "activity_version": 1})

    with pytest.raises(ValueError):
        validate_learning_path(path)


@pytest.mark.django_db
def test_unpublished_activity_version_is_rejected(definition, registry):
    unpublished = create_activity_version(definition, config={})
    path = _make_tree(
        {
            "activity_public_id": str(definition.public_id),
            "activity_version": unpublished.version,
        }
    )

    with pytest.raises(ValueError):
        validate_learning_path(path)


@pytest.mark.django_db
def test_wrong_version_number_is_rejected(published_version):
    path = _make_tree(
        {
            "activity_public_id": str(published_version.definition.public_id),
            "activity_version": published_version.version + 1,
        }
    )

    with pytest.raises(ValueError):
        validate_learning_path(path)
