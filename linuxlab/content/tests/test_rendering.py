import uuid

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from wagtail.models import Page

from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.core.models import DomainEvent


def _root():
    return Page.get_first_root_node()


def _build_module():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    return module


@pytest.mark.django_db
def test_lesson_page_renders_representative_streamfield_content():
    module = _build_module()
    activity_public_id = str(uuid.uuid4())
    lesson = LessonPage(
        title="The ls command",
        body=[
            ("heading", "Listing files"),
            ("text", "<p>The ls command lists directory contents.</p>"),
            ("command", {"command": "ls -la", "expected_output": "total 0"}),
            (
                "tip",
                {"heading": "Tip", "body": "<p>Use -a to show hidden files.</p>"},
            ),
            (
                "activity",
                {"activity_public_id": activity_public_id, "activity_version": 2},
            ),
        ],
    )
    module.add_child(instance=lesson)

    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    response = lesson.serve(request)
    response.render()
    content = response.content.decode()

    assert response.status_code == 200
    assert "The ls command" in content
    assert "Listing files" in content
    assert "ls -la" in content
    assert "Use -a to show hidden files" in content


@pytest.mark.django_db
def test_lesson_streamfield_round_trips_activity_reference_through_database():
    module = _build_module()
    activity_public_id = str(uuid.uuid4())
    lesson = LessonPage(
        title="The ls command",
        body=[
            (
                "activity",
                {"activity_public_id": activity_public_id, "activity_version": 2},
            )
        ],
    )
    module.add_child(instance=lesson)

    reloaded = LessonPage.objects.get(pk=lesson.pk)
    activity_value = reloaded.body[0].value

    assert activity_value["activity_public_id"] == activity_public_id
    assert activity_value["activity_version"] == 2


@pytest.mark.django_db
def test_content_module_does_not_write_domain_events_as_a_side_effect():
    before = DomainEvent.objects.count()

    module = _build_module()
    lesson = LessonPage(title="The ls command", body=[("heading", "Hi")])
    module.add_child(instance=lesson)

    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    response = lesson.serve(request)
    response.render()

    assert DomainEvent.objects.count() == before
