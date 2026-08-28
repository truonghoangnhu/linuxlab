import uuid

import pytest
from django.core.exceptions import ValidationError
from wagtail.models import Page

from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage


def _root():
    return Page.get_first_root_node()


@pytest.mark.django_db
def test_learning_path_page_receives_uuid_public_id():
    page = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=page)

    assert isinstance(page.public_id, uuid.UUID)


@pytest.mark.django_db
def test_module_page_receives_uuid_public_id():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)

    assert isinstance(module.public_id, uuid.UUID)


@pytest.mark.django_db
def test_lesson_page_receives_uuid_public_id():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    module.add_child(instance=lesson)

    assert isinstance(lesson.public_id, uuid.UUID)


@pytest.mark.django_db
def test_different_pages_receive_different_public_ids():
    first = LearningPathPage(title="Linux Basics")
    second = LearningPathPage(title="Networking Basics")
    _root().add_child(instance=first)
    _root().add_child(instance=second)

    assert first.public_id != second.public_id


@pytest.mark.django_db
def test_public_id_is_separate_from_local_primary_key():
    page = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=page)

    assert page.pk is not None
    assert str(page.public_id) != str(page.pk)


def test_public_id_field_is_not_editable():
    field = LearningPathPage._meta.get_field("public_id")

    assert field.editable is False


def test_public_id_field_is_unique():
    field = LearningPathPage._meta.get_field("public_id")

    assert field.unique is True


@pytest.mark.django_db
def test_public_id_uniqueness_is_enforced_by_the_model_field_contract():
    page = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=page)

    duplicate = LearningPathPage(title="Duplicate", public_id=page.public_id)

    with pytest.raises(ValidationError):
        _root().add_child(instance=duplicate)
