import uuid

from linuxlab.activities.models import ActivityVersion
from linuxlab.activities.services import get_activity_version


def _validate_activity_reference(value, activity_registry):
    """Confirm one ActivityReferenceBlock resolves to an EXACT published
    ActivityVersion (ADR-004, ADR-009) -- never "latest" -- and that its
    provider still accepts its immutable config today.

    Task 09 correction (transitive QuestionVersion dependency, Critical/
    Correction D): re-checking `published_at IS NOT NULL` alone is not
    enough, because an ActivityVersion can have been marked published by
    `publish_activity_version()` using a DIFFERENT, more lenient registry
    than the one this publication run was given -- that older call's
    provider.validate_config() may never have existed or may not have
    checked question dependencies at all. Re-running THIS registry's
    provider.validate_config() against the immutable persisted config
    re-establishes the guarantee independently of how/when the
    ActivityVersion was originally published, reusing the existing
    AssessmentActivityProvider boundary (which already resolves and
    requires an exact published QuestionVersion per question reference)
    rather than duplicating assessment internals here.
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
        activity_version = get_activity_version(activity_public_id, version)
    except ActivityVersion.DoesNotExist as exc:
        raise ValueError(
            f"no published ActivityVersion for {activity_public_id} v{version}"
        ) from exc

    try:
        provider = activity_registry.get(activity_version.definition.type_key)
    except LookupError as exc:
        raise ValueError(
            f"no provider registered for type_key "
            f"'{activity_version.definition.type_key}'"
        ) from exc

    provider.validate_config(activity_version.config)


def validate_captured_learning_path(capture, activity_registry):
    """Validate a capture_learning_path() structure -- the frozen tree
    captured under lock -- never the live Wagtail tree.

    Task 09 correction (TOCTOU, Critical 2): validation and persistence must
    observe exactly the same captured state, so this never reads Wagtail
    pages again; it only walks the plain dict/list structure it was given.
    """
    for module in capture["learning_path"]["modules"]:
        for lesson in module["lessons"]:
            for block in lesson["blocks"]:
                if block["type"] == "activity":
                    _validate_activity_reference(block["value"], activity_registry)


def validate_learning_path(learning_path_page, activity_registry):
    """Standalone convenience validator: captures a fresh coherent snapshot
    of the tree under lock and validates it in one step.

    publish_learning_path() does NOT call this -- it performs its own single
    capture_learning_path() call and reuses that exact structure for both
    validation and persistence, so validation and the persisted payload can
    never diverge (Task 09 correction, Critical 2).
    """
    from linuxlab.publication.snapshots import capture_learning_path

    capture = capture_learning_path(learning_path_page)
    validate_captured_learning_path(capture, activity_registry)
