_CONTENT_KEYS = {"prompt", "options"}
_OPTION_KEYS = {"id", "text"}
_ANSWER_KEY_KEYS = {"correct_option_id"}
_SCORING_KEYS = {"max_score"}


def _require_exact_keys(value, allowed_keys, label):
    if not isinstance(value, dict):
        # ValueError, not TypeError: untrusted content/answer_key/scoring shape
        # validation, part of the same domain-error contract as every other
        # check in this module.
        raise ValueError(f"{label} must be an object")  # noqa: TRY004

    actual_keys = set(value.keys())
    extra = actual_keys - allowed_keys
    if extra:
        raise ValueError(f"{label} contains unexpected fields: {sorted(extra)}")
    missing = allowed_keys - actual_keys
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")


class SingleChoiceProvider:
    """Question-type provider for single-correct-answer multiple choice.

    content = {"prompt": str, "options": [{"id": str, "text": str}, ...]}
    answer_key = {"correct_option_id": str}
    scoring = {"max_score": positive int}
    response = {"selected_option_id": str}

    Every schema is an exact allow-list: unknown fields are rejected outright
    so grading data (answer_key, is_correct, ...) can never be smuggled into
    learner-visible content, and content can never be smuggled into
    answer_key/scoring (Task 06 correction).
    """

    type_key = "single_choice"

    def validate(self, content, answer_key, scoring):
        _require_exact_keys(content, _CONTENT_KEYS, "content")

        prompt = content["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        options = content["options"]
        if not isinstance(options, list) or len(options) < 2:
            raise ValueError("options must be a list with at least two entries")

        seen_ids = set()
        for option in options:
            _require_exact_keys(option, _OPTION_KEYS, "option")

            option_id = option["id"]
            text = option["text"]
            if not isinstance(option_id, str) or not option_id.strip():
                raise ValueError("each option must have a non-empty id")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("each option must have non-empty text")
            if option_id in seen_ids:
                raise ValueError(f"duplicate option id '{option_id}'")
            seen_ids.add(option_id)

        _require_exact_keys(answer_key, _ANSWER_KEY_KEYS, "answer_key")

        correct_option_id = answer_key["correct_option_id"]
        if not isinstance(correct_option_id, str) or not correct_option_id.strip():
            raise ValueError("answer_key must contain a non-empty correct_option_id")
        if correct_option_id not in seen_ids:
            raise ValueError("correct_option_id must reference an existing option")

        _require_exact_keys(scoring, _SCORING_KEYS, "scoring")

        max_score = scoring["max_score"]
        if type(max_score) is not int or max_score < 1:
            raise ValueError("scoring.max_score must be a positive integer")

    def learner_payload(self, content):
        return {
            "type": self.type_key,
            "prompt": content["prompt"],
            "options": [
                {"id": option["id"], "text": option["text"]} for option in content["options"]
            ],
        }

    def evaluate(self, content, answer_key, scoring, response):
        correct_option_id = answer_key["correct_option_id"]
        selected_option_id = response.get("selected_option_id") if isinstance(response, dict) else None
        correct = selected_option_id == correct_option_id
        max_score = scoring["max_score"]
        return {
            "correct": correct,
            "score": max_score if correct else 0,
            "max_score": max_score,
        }
