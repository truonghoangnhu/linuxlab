class SingleChoiceProvider:
    """Question-type provider for single-correct-answer multiple choice.

    content = {"prompt": str, "options": [{"id": str, "text": str}, ...]}
    answer_key = {"correct_option_id": str}
    response = {"selected_option_id": str}
    """

    type_key = "single_choice"

    def validate(self, content, answer_key):
        prompt = content.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        options = content.get("options")
        if not isinstance(options, list) or len(options) < 2:
            raise ValueError("options must be a list with at least two entries")

        seen_ids = set()
        for option in options:
            if not isinstance(option, dict):
                # ValueError, not TypeError: this is untrusted content shape
                # validation, part of the same domain-error contract as every
                # other check in this method.
                raise ValueError("each option must be an object")  # noqa: TRY004
            option_id = option.get("id")
            text = option.get("text")
            if not isinstance(option_id, str) or not option_id.strip():
                raise ValueError("each option must have a non-empty id")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("each option must have non-empty text")
            if option_id in seen_ids:
                raise ValueError(f"duplicate option id '{option_id}'")
            seen_ids.add(option_id)

        correct_option_id = answer_key.get("correct_option_id")
        if not isinstance(correct_option_id, str) or not correct_option_id.strip():
            raise ValueError("answer_key must contain a non-empty correct_option_id")
        if correct_option_id not in seen_ids:
            raise ValueError("correct_option_id must reference an existing option")

    def learner_payload(self, content):
        return {
            "type": self.type_key,
            "prompt": content["prompt"],
            "options": [
                {"id": option["id"], "text": option["text"]} for option in content["options"]
            ],
        }

    def evaluate(self, content, answer_key, response):
        correct_option_id = answer_key["correct_option_id"]
        selected_option_id = response.get("selected_option_id") if isinstance(response, dict) else None
        correct = selected_option_id == correct_option_id
        return {
            "correct": correct,
            "score": 1 if correct else 0,
            "max_score": 1,
        }
