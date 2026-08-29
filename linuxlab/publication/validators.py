import uuid

from linuxlab.activities.models import ActivityVersion
from linuxlab.activities.services import get_activity_version
from linuxlab.content.models import LessonPage, ModulePage


def _validate_activity_reference(value):
    """Confirm one ActivityReferenceBlock resolves to an EXACT published
    ActivityVersion (ADR-004, ADR-009) -- never "latest". QuestionVersion
    dependencies are intentionally NOT re-checked here: an already-published
    ActivityVersion's own dependencies were already validated by the Task
    05/06 ActivityProvider boundary at ITS publish time, and both
    ActivityVersion and QuestionVersion are immutable once published, so
    re-validating them here would be redundant coupling into assessment
    internals that an existing boundary already owns.
    """
    raw_public_id = value.get("activity_public_id") if hasattr(value, "get") else None
    try:
        activity_public_id = uuid.UUID(str(raw_public_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("activity reference has an invalid activity_public_id") from exc

    version = value.get("activity_version") if hasattr(value, "get") else None
    # type(x) is int, not isinstance: isinstance(True, int) is True in
    # Python, which would let activity_version=True silently match version 1.
    if type(version) is not int or version < 1:
        raise ValueError("activity reference has an invalid activity_version")

    try:
        get_activity_version(activity_public_id, version)
    except ActivityVersion.DoesNotExist as exc:
        raise ValueError(
            f"no published ActivityVersion for {activity_public_id} v{version}"
        ) from exc


def validate_learning_path(learning_path_page):
    """Walk LearningPathPage -> ModulePage -> LessonPage and confirm every
    activity reference resolves to an exact published ActivityVersion.
    Fails closed on the first missing/unpublished/malformed reference.
    """
    modules = ModulePage.objects.child_of(learning_path_page).specific().order_by("path")
    for module in modules:
        lessons = LessonPage.objects.child_of(module).specific().order_by("path")
        for lesson in lessons:
            for block in lesson.body:
                if block.block_type == "activity":
                    _validate_activity_reference(block.value)
