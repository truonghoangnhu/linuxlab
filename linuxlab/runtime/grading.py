"""Task 13 server-only grading runtime service (T13-D06, T13-D07).

Deliberately kept in its own module, separate from content.py: the
learner-facing linuxlab.runtime facade (__init__.py) must not export
get_runtime_question_grading() -- a caller that needs grading data must
explicitly `from linuxlab.runtime.grading import
get_runtime_question_grading`, never receive it as a byproduct of
importing the learner-safe facade. There is no combined learner+grading
DTO: this always returns RuntimeQuestionGrading, never RuntimeQuestion.
"""

from linuxlab.sync.models import InstalledQuestionGrading, InstalledQuestionVersion

from .context import _rebind_runtime_context
from .dto import RuntimeQuestionGrading
from .exceptions import RuntimeArtifactNotFound
from .freeze import freeze_json_value


def get_runtime_question_grading(context, question_public_id, version):
    """Return the exact (question_public_id, version) grading row owned
    by the pinned package, as a frozen RuntimeQuestionGrading. Follows
    the same context-rebind and package-scoping rules as the learner
    content services in content.py."""
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

    grading = InstalledQuestionGrading.objects.filter(question=question).first()
    if grading is None:
        raise RuntimeArtifactNotFound(
            f"no grading for question {question_public_id} v{version} in "
            f"InstalledPackage {package.pk}"
        )

    return RuntimeQuestionGrading(
        question_public_id=question.question_public_id,
        version=question.version,
        answer_key=freeze_json_value(grading.answer_key),
        scoring=freeze_json_value(grading.scoring),
    )
