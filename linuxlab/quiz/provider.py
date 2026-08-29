import uuid

from linuxlab.assessment.models import QuestionVersion
from linuxlab.assessment.services import get_question_version, serialize_question_for_learner

_CONFIG_KEYS = {"questions"}
_QUESTION_REF_KEYS = {"question_public_id", "question_version"}


def _is_exact_version(value):
    # type(x) is int, not isinstance(x, int): isinstance(True, int) is True in
    # Python, which would let question_version=True silently match version 1.
    return type(value) is int and value >= 1


def _parse_question_ref(item):
    if not isinstance(item, dict):
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

    def evaluate(self, config, submission):
        responses_by_key = self._parse_responses(submission)

        results = []
        total_score = 0
        total_max_score = 0

        for item in config["questions"]:
            question_public_id, version = _parse_question_ref(item)
            question_version = get_question_version(question_public_id, version)
            provider = self.question_registry.get(question_version.type_key)

            response = responses_by_key.get((question_public_id, version), {})
            outcome = provider.evaluate(
                question_version.content,
                question_version.answer_key,
                question_version.scoring,
                response,
            )

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
