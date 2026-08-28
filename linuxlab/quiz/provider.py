import uuid

from linuxlab.assessment.models import QuestionVersion
from linuxlab.assessment.services import get_question_version, serialize_question_for_learner


def _parse_question_ref(item):
    if not isinstance(item, dict):
        # ValueError, not TypeError: untrusted config shape validation, same
        # domain-error contract as the rest of this module.
        raise ValueError("each question reference must be an object")  # noqa: TRY004

    raw_public_id = item.get("question_public_id")
    try:
        question_public_id = uuid.UUID(str(raw_public_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("question_public_id must be a valid UUID") from exc

    version = item.get("question_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("question_version must be an integer >= 1")

    return question_public_id, version


class AssessmentActivityProvider:
    """Concrete Task 05 ActivityProvider wiring the shared assessment engine
    into the Activity extension boundary (ADR-004, ADR-009, ADR-013).

    config = {"questions": [{"question_public_id": "<uuid>", "question_version": 1}, ...]}
    submission = {"responses": [{"question_public_id": "<uuid>", "question_version": 1,
                                  "selected_option_id": "..."}, ...]}

    Answer keys never enter config or the evaluation result; they are read
    server-side, exactly once per question, from the published QuestionVersion.
    """

    type_key = "assessment"

    def __init__(self, question_registry):
        self.question_registry = question_registry

    def validate_config(self, config):
        if not isinstance(config, dict):
            raise ValueError("config must be an object")  # noqa: TRY004

        questions = config.get("questions")
        if not isinstance(questions, list) or not questions:
            raise ValueError("config must contain a non-empty 'questions' list")

        for item in questions:
            question_public_id, version = _parse_question_ref(item)

            try:
                question_version = get_question_version(question_public_id, version)
            except QuestionVersion.DoesNotExist as exc:
                raise ValueError(
                    f"no published QuestionVersion for {question_public_id} v{version}"
                ) from exc

            # confirm a provider is registered for this question's type
            self.question_registry.get(question_version.question.type_key)

    def evaluate(self, config, submission):
        responses = submission.get("responses", []) if isinstance(submission, dict) else []
        responses_by_key = {
            (str(response.get("question_public_id")), response.get("question_version")): response
            for response in responses
            if isinstance(response, dict)
        }

        results = []
        total_score = 0
        total_max_score = 0

        for item in config["questions"]:
            question_public_id, version = _parse_question_ref(item)
            question_version = get_question_version(question_public_id, version)
            provider = self.question_registry.get(question_version.question.type_key)

            response = responses_by_key.get((str(question_public_id), version), {})
            outcome = provider.evaluate(
                question_version.content, question_version.answer_key, response
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


def serialize_assessment_for_learner(activity_version, question_registry):
    if not activity_version.is_published:
        raise ValueError("ActivityVersion is not published")

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
