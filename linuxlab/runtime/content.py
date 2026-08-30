"""Task 13 learner-safe runtime content services (T13-D03, T13-D06):
publication/activity/question reads scoped to one pinned InstalledPackage.
Never touches grading -- see grading.py, a deliberately separate module.
"""

from linuxlab.sync.models import (
    InstalledActivityVersion,
    InstalledPublication,
    InstalledQuestionVersion,
)

from .context import _rebind_runtime_context
from .dto import RuntimeActivity, RuntimePublication, RuntimeQuestion
from .exceptions import RuntimeArtifactNotFound
from .freeze import freeze_json_value


def get_runtime_publication(context):
    """Return the exactly-one InstalledPublication owned by the pinned
    package, as a frozen RuntimePublication."""
    package = _rebind_runtime_context(context)

    publication = InstalledPublication.objects.filter(package=package).first()
    if publication is None:
        raise RuntimeArtifactNotFound(
            f"InstalledPackage {package.pk} has no staged publication artifact"
        )

    return RuntimePublication(
        kind=publication.kind,
        source_public_id=publication.source_public_id,
        version=publication.version,
        content_hash=publication.content_hash,
        payload=freeze_json_value(publication.payload),
    )


def get_runtime_activity(context, activity_public_id, version):
    """Return the exact (activity_public_id, version) activity owned by
    the pinned package. No `latest` lookup; not visible across packages."""
    package = _rebind_runtime_context(context)

    activity = InstalledActivityVersion.objects.filter(
        package=package,
        activity_public_id=activity_public_id,
        version=version,
    ).first()
    if activity is None:
        raise RuntimeArtifactNotFound(
            f"no activity {activity_public_id} v{version} in InstalledPackage {package.pk}"
        )

    return RuntimeActivity(
        activity_public_id=activity.activity_public_id,
        version=activity.version,
        type_key=activity.type_key,
        config=freeze_json_value(activity.config),
    )


def get_runtime_question(context, question_public_id, version):
    """Return the exact (question_public_id, version) learner-safe
    question owned by the pinned package. Never includes answer_key or
    scoring -- see linuxlab.runtime.grading for that."""
    package = _rebind_runtime_context(context)

    question = InstalledQuestionVersion.objects.filter(
        package=package,
        question_public_id=question_public_id,
        version=version,
    ).first()
    if question is None:
        raise RuntimeArtifactNotFound(
            f"no question {question_public_id} v{version} in InstalledPackage {package.pk}"
        )

    return RuntimeQuestion(
        question_public_id=question.question_public_id,
        version=question.version,
        type_key=question.type_key,
        content=freeze_json_value(question.content),
    )
