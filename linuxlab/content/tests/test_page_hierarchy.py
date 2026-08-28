import pytest
from wagtail.models import Page

from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage


def _root():
    return Page.get_first_root_node()


@pytest.mark.django_db
def test_learning_path_allowed_under_root():
    assert LearningPathPage.can_create_at(_root())


@pytest.mark.django_db
def test_module_allowed_under_learning_path():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)

    assert ModulePage.can_create_at(path)


@pytest.mark.django_db
def test_lesson_allowed_under_module():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)

    assert LessonPage.can_create_at(module)


@pytest.mark.django_db
def test_lesson_denied_directly_under_learning_path():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)

    assert not LessonPage.can_create_at(path)


@pytest.mark.django_db
def test_module_denied_under_module():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)

    assert not ModulePage.can_create_at(module)


@pytest.mark.django_db
def test_learning_path_denied_under_module():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)

    assert not LearningPathPage.can_create_at(module)


@pytest.mark.django_db
def test_lesson_page_has_no_creatable_subpage_types():
    assert LessonPage.creatable_subpage_models() == []


@pytest.mark.django_db
def test_real_tree_creation_matches_allowed_hierarchy():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    module.add_child(instance=lesson)

    assert lesson.get_parent().specific == module
    assert module.get_parent().specific == path
