import uuid
from collections.abc import Mapping

from linuxlab.activities.contracts import ActivityDependencies
from linuxlab.assessment.models import QuestionVersion
from linuxlab.assessment.services import get_question_version, serialize_question_for_learner

_CONFIG_KEYS = {"questions", "completion_threshold"}
_QUESTION_REF_KEYS = {"question_public_id", "question_version"}


def _is_exact_version(value):
    # type(x) is int, not isinstance(x, int): isinstance(True, int) is True in
    # Python, which would let question_version=True silently match version 1.
    return type(value) is int and value >= 1


def _validate_completion_threshold(value):
    # type(x) in (int, float), not isinstance: isinstance(True, int) is True
    # in Python, which would let completion_threshold=True silently pass as
    # 1.0 (Task 07 corrective). Range/type semantics match the only existing
    # convention: the generic learning-state projection reads this as a
    # score/max_score ratio in [0, 1] (linuxlab.learning.projections).
    if type(value) not in (int, float):
        raise ValueError("completion_threshold must be a number")
    if not (0 <= value <= 1):
        raise ValueError("completion_threshold must be between 0 and 1")


def _parse_question_ref(item):
    # Mapping, not dict: config may arrive either as an ordinary dict
    # (Central authoring/validation) or as a frozen MappingProxyType (Node
    # RuntimeActivity.config, via linuxlab.runtime.freeze.freeze_json_value
    # -- T15-D27). Both satisfy Mapping; dict does not satisfy
    # MappingProxyType's own isinstance check, so this must not be
    # narrowed back to dict.
    if not isinstance(item, Mapping):
        # ValueError, not TypeError: untrusted config shape validation, same
        # domain-error contract as the rest of this module.
        raise ValueError("each question reference must be an object")  # noqa: TRY004

    actual_keys = set(item.keys())
    extra = actual_keys - _QUESTION_REF_KEYS
    if extra:
        raise ValueError(f"question reference contains unexpected fields: {sorted(extra)}")
    missing = _QUESTION_REF_KEYS - actual_keys
    if missing:
        raise ValueError(f"question reference is missing fields: {sorted(missing)}")

    raw_public_id = item["question_public_id"]
    try:
        question_public_id = uuid.UUID(str(raw_public_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("question_public_id must be a valid UUID") from exc

    version = item["question_version"]
    if not _is_exact_version(version):
        raise ValueError("question_version must be an integer >= 1")

    return question_public_id, version


class AssessmentActivityProvider:
    """Concrete Task 05 ActivityProvider wiring the shared assessment engine
    into the Activity extension boundary (ADR-004, ADR-009, ADR-013).

    config = {"questions": [{"question_public_id": "<uuid>", "question_version": 1}, ...]}
    submission = {"responses": [{"question_public_id": "<uuid>", "question_version": 1,
                                  "selected_option_id": "..."}, ...]}

    Both schemas are exact allow-lists: config/reference fields beyond the
    ones above are rejected outright, so config can never become a second
    answer-key store. Answer keys and scoring never enter config or the
    evaluation result; they are read server-side, exactly once per question,
    from the published QuestionVersion that owns them (Task 06 correction).
    """

    type_key = "assessment"

    def __init__(self, question_registry):
        self.question_registry = question_registry

    def validate_config(self, config):
        if not isinstance(config, dict):
            raise ValueError("config must be an object")  # noqa: TRY004

        extra_top_level = set(config.keys()) - _CONFIG_KEYS
        if extra_top_level:
            raise ValueError(f"config contains unexpected fields: {sorted(extra_top_level)}")

        questions = config.get("questions")
        if not isinstance(questions, list) or not questions:
            raise ValueError("config must contain a non-empty 'questions' list")

        if "completion_threshold" in config:
            _validate_completion_threshold(config["completion_threshold"])

        seen_refs = set()
        for item in questions:
            question_public_id, version = _parse_question_ref(item)

            ref_key = (question_public_id, version)
            if ref_key in seen_refs:
                raise ValueError(f"duplicate question reference {question_public_id} v{version}")
            seen_refs.add(ref_key)

            try:
                question_version = get_question_version(question_public_id, version)
            except QuestionVersion.DoesNotExist as exc:
                raise ValueError(
                    f"no published QuestionVersion for {question_public_id} v{version}"
                ) from exc

            # confirm a provider is registered for this exact version's type
            self.question_registry.get(question_version.type_key)

    def collect_dependencies(self, config):
        """Exact QuestionVersion references this config depends on (Task 11
        package dependency closure). Reuses _parse_question_ref -- the same
        parsing validate_config() already applies -- rather than
        duplicating assessment config parsing in the packaging layer.
        """
        refs = tuple(_parse_question_ref(item) for item in config["questions"])
        return ActivityDependencies(question_versions=refs)

    def evaluate(self, config, submission, context=None):
        """context=None (default) resolves each question from the Central
        live-authoring QuestionVersion table -- the original Task 06/07
        behavior, unchanged for every existing caller.

        T15-D27 S8: a caller grading a Node-installed Attempt instead
        passes context=<RuntimePackageContext>, pinned to the exact
        InstalledPackage the Attempt started against. Resolution then
        reads exclusively from that package's InstalledQuestionVersion/
        InstalledQuestionGrading rows -- never the live authoring tables --
        so a Node execution path can never fetch or reconstruct live
        authoring question versions. The QuestionProvider.evaluate() call
        below is unchanged either way: it is already resolver-agnostic,
        taking plain content/answer_key/scoring/response values.
        """
        responses_by_key = self._parse_responses(submission)

        results = []
        total_score = 0
        total_max_score = 0

        for item in config["questions"]:
            question_public_id, version = _parse_question_ref(item)
            type_key, content, answer_key, scoring = self._resolve_question(
                context, question_public_id, version
            )
            provider = self.question_registry.get(type_key)

            response = responses_by_key.get((question_public_id, version), {})
            outcome = provider.evaluate(content, answer_key, scoring, response)

            total_score += outcome["score"]
            total_max_score += outcome["max_score"]
            results.append(
                {
                    "question_public_id": str(question_public_id),
                    "question_version": version,
                    "correct": outcome["correct"],
                    "score": outcome["score"],
                    "max_score": outcome["max_score"],
                }
            )

        return {
            "score": total_score,
            "max_score": total_max_score,
            "results": results,
        }

    @staticmethod
    def _resolve_question(context, question_public_id, version):
        """Returns (type_key, content, answer_key, scoring) for one exact
        question reference, from either the Central live-authoring table
        (context is None) or the pinned Node InstalledPackage (context is
        a RuntimePackageContext) -- see evaluate()'s docstring."""
        if context is None:
            question_version = get_question_version(question_public_id, version)
            return (
                question_version.type_key,
                question_version.content,
                question_version.answer_key,
                question_version.scoring,
            )

        # Local import: keeps linuxlab.quiz's dependency on linuxlab.runtime
        # confined to this one Node-only code path, not module-level.
        from linuxlab.runtime.content import get_runtime_question
        from linuxlab.runtime.grading import get_runtime_question_grading

        runtime_question = get_runtime_question(context, question_public_id, version)
        runtime_grading = get_runtime_question_grading(context, question_public_id, version)
        return (
            runtime_question.type_key,
            runtime_question.content,
            runtime_grading.answer_key,
            runtime_grading.scoring,
        )

    @staticmethod
    def _parse_responses(submission):
        responses = submission.get("responses", []) if isinstance(submission, dict) else []

        responses_by_key = {}
        for response in responses:
            if not isinstance(response, dict):
                continue

            raw_public_id = response.get("question_public_id")
            try:
                question_public_id = uuid.UUID(str(raw_public_id))
            except (ValueError, AttributeError, TypeError):
                # malformed identity: cannot match any configured question,
                # handled deterministically (as "no response") rather than
                # raising on untrusted learner input.
                continue

            version = response.get("question_version")
            if not _is_exact_version(version):
                continue

            key = (question_public_id, version)
            if key in responses_by_key:
                raise ValueError(
                    f"duplicate response for question {question_public_id} v{version}"
                )
            responses_by_key[key] = response

        return responses_by_key


def serialize_assessment_for_learner(activity_version, question_registry):
    if not activity_version.is_published:
        raise ValueError("ActivityVersion is not published")
    if activity_version.definition.type_key != AssessmentActivityProvider.type_key:
        raise ValueError("ActivityVersion is not an assessment activity")

    questions = []
    for item in activity_version.config["questions"]:
        question_public_id, version = _parse_question_ref(item)
        question_version = get_question_version(question_public_id, version)
        questions.append(serialize_question_for_learner(question_version, question_registry))

    return {
        "activity_public_id": str(activity_version.definition.public_id),
        "activity_version": activity_version.version,
        "questions": questions,
    }


def serialize_installed_assessment_for_learner(context, activity, question_registry):
    """Node counterpart to serialize_assessment_for_learner() (T15-D27
    S8, S11): renders a learner-safe payload for an installed
    (RuntimeActivity) assessment activity, resolving each question from
    the pinned InstalledPackage via linuxlab.runtime.content -- never the
    live authoring QuestionVersion table.
    """
    if activity.type_key != AssessmentActivityProvider.type_key:
        raise ValueError("InstalledActivityVersion is not an assessment activity")

    # Local import: same Node-only-path reasoning as evaluate()/
    # _resolve_question() above.
    from linuxlab.runtime.content import get_runtime_question

    questions = []
    for item in activity.config["questions"]:
        question_public_id, version = _parse_question_ref(item)
        runtime_question = get_runtime_question(context, question_public_id, version)
        provider = question_registry.get(runtime_question.type_key)
        payload = provider.learner_payload(runtime_question.content)

        if "question_public_id" in payload or "question_version" in payload:
            raise ValueError(
                "provider learner_payload must not define question_public_id/question_version"
            )

        questions.append(
            {
                **payload,
                "question_public_id": str(runtime_question.question_public_id),
                "question_version": runtime_question.version,
            }
        )

    return {
        "activity_public_id": str(activity.activity_public_id),
        "activity_version": activity.version,
        "questions": questions,
    }
