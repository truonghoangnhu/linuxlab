import json

import pytest

from linuxlab.assessment.providers import SingleChoiceProvider

VALID_CONTENT = {
    "prompt": "Which command lists files?",
    "options": [
        {"id": "a", "text": "ls"},
        {"id": "b", "text": "rm"},
    ],
}
VALID_ANSWER_KEY = {"correct_option_id": "a"}
VALID_SCORING = {"max_score": 1}


def _provider():
    return SingleChoiceProvider()


def _validate(content=VALID_CONTENT, answer_key=VALID_ANSWER_KEY, scoring=VALID_SCORING):
    _provider().validate(content, answer_key, scoring)


# --- validation: valid ------------------------------------------------


def test_valid_content_answer_key_and_scoring_pass_validation():
    _validate()


def test_more_than_two_options_is_valid():
    content = {
        "prompt": "Pick one",
        "options": [
            {"id": "a", "text": "A"},
            {"id": "b", "text": "B"},
            {"id": "c", "text": "C"},
        ],
    }
    _validate(content=content, answer_key={"correct_option_id": "c"})


def test_scoring_with_higher_max_score_is_valid():
    _validate(scoring={"max_score": 5})


# --- validation: invalid content --------------------------------------


def test_empty_prompt_is_rejected():
    content = {**VALID_CONTENT, "prompt": ""}
    with pytest.raises(ValueError):
        _validate(content=content)


def test_fewer_than_two_options_is_rejected():
    content = {"prompt": "Pick one", "options": [{"id": "a", "text": "A"}]}
    with pytest.raises(ValueError):
        _validate(content=content, answer_key={"correct_option_id": "a"})


def test_duplicate_option_ids_are_rejected():
    content = {
        "prompt": "Pick one",
        "options": [
            {"id": "a", "text": "A"},
            {"id": "a", "text": "A again"},
        ],
    }
    with pytest.raises(ValueError):
        _validate(content=content, answer_key={"correct_option_id": "a"})


def test_missing_option_id_is_rejected():
    content = {
        "prompt": "Pick one",
        "options": [
            {"text": "A"},
            {"id": "b", "text": "B"},
        ],
    }
    with pytest.raises(ValueError):
        _validate(content=content, answer_key={"correct_option_id": "b"})


def test_missing_option_text_is_rejected():
    content = {
        "prompt": "Pick one",
        "options": [
            {"id": "a"},
            {"id": "b", "text": "B"},
        ],
    }
    with pytest.raises(ValueError):
        _validate(content=content, answer_key={"correct_option_id": "b"})


def test_answer_referencing_nonexistent_option_is_rejected():
    with pytest.raises(ValueError):
        _validate(answer_key={"correct_option_id": "does-not-exist"})


# --- validation: exact allow-list schemas (mandatory security tests) -----


def test_unknown_top_level_content_field_is_rejected():
    content = {**VALID_CONTENT, "extra_field": "not allowed"}
    with pytest.raises(ValueError):
        _validate(content=content)


def test_content_containing_answer_key_is_rejected():
    content = {**VALID_CONTENT, "answer_key": {"correct_option_id": "SECRET"}}
    with pytest.raises(ValueError):
        _validate(content=content)


def test_content_containing_correct_option_id_is_rejected():
    content = {**VALID_CONTENT, "correct_option_id": "SECRET"}
    with pytest.raises(ValueError):
        _validate(content=content)


def test_option_with_is_correct_field_is_rejected():
    content = {
        "prompt": "Pick one",
        "options": [
            {"id": "a", "text": "A", "is_correct": True},
            {"id": "b", "text": "B"},
        ],
    }
    with pytest.raises(ValueError):
        _validate(content=content)


def test_option_with_correct_field_is_rejected():
    content = {
        "prompt": "Pick one",
        "options": [
            {"id": "a", "text": "A", "correct": True},
            {"id": "b", "text": "B"},
        ],
    }
    with pytest.raises(ValueError):
        _validate(content=content)


def test_unknown_answer_key_field_is_rejected():
    answer_key = {**VALID_ANSWER_KEY, "extra": "field"}
    with pytest.raises(ValueError):
        _validate(answer_key=answer_key)


def test_answer_key_missing_correct_option_id_is_rejected():
    with pytest.raises(ValueError):
        _validate(answer_key={})


def test_non_dict_content_is_rejected():
    with pytest.raises(ValueError):
        _validate(content=["not", "a", "dict"])


def test_non_dict_answer_key_is_rejected():
    with pytest.raises(ValueError):
        _validate(answer_key="not-a-dict")


# --- validation: scoring ------------------------------------------------


def test_unknown_scoring_field_is_rejected():
    with pytest.raises(ValueError):
        _validate(scoring={"max_score": 1, "bonus": 5})


def test_scoring_missing_max_score_is_rejected():
    with pytest.raises(ValueError):
        _validate(scoring={})


def test_scoring_zero_max_score_is_rejected():
    with pytest.raises(ValueError):
        _validate(scoring={"max_score": 0})


def test_scoring_negative_max_score_is_rejected():
    with pytest.raises(ValueError):
        _validate(scoring={"max_score": -1})


def test_scoring_boolean_max_score_is_rejected():
    with pytest.raises(ValueError):
        _validate(scoring={"max_score": True})


def test_non_dict_scoring_is_rejected():
    with pytest.raises(ValueError):
        _validate(scoring="not-a-dict")


# --- evaluation -----------------------------------------------------------


def test_evaluate_correct_response():
    result = _provider().evaluate(
        VALID_CONTENT, VALID_ANSWER_KEY, VALID_SCORING, {"selected_option_id": "a"}
    )

    assert result["correct"] is True
    assert result["score"] == 1
    assert result["max_score"] == 1


def test_evaluate_incorrect_response():
    result = _provider().evaluate(
        VALID_CONTENT, VALID_ANSWER_KEY, VALID_SCORING, {"selected_option_id": "b"}
    )

    assert result["correct"] is False
    assert result["score"] == 0
    assert result["max_score"] == 1


def test_evaluate_uses_versioned_scoring():
    scoring = {"max_score": 3}

    correct_result = _provider().evaluate(
        VALID_CONTENT, VALID_ANSWER_KEY, scoring, {"selected_option_id": "a"}
    )
    incorrect_result = _provider().evaluate(
        VALID_CONTENT, VALID_ANSWER_KEY, scoring, {"selected_option_id": "b"}
    )

    assert correct_result["score"] == 3
    assert correct_result["max_score"] == 3
    assert incorrect_result["score"] == 0
    assert incorrect_result["max_score"] == 3


def test_evaluate_malformed_response_is_handled_deterministically():
    result_none = _provider().evaluate(VALID_CONTENT, VALID_ANSWER_KEY, VALID_SCORING, None)
    result_empty = _provider().evaluate(VALID_CONTENT, VALID_ANSWER_KEY, VALID_SCORING, {})
    result_unknown_key = _provider().evaluate(
        VALID_CONTENT, VALID_ANSWER_KEY, VALID_SCORING, {"unexpected": "x"}
    )

    for result in (result_none, result_empty, result_unknown_key):
        assert result["correct"] is False
        assert result["score"] == 0
        assert result["max_score"] == 1


# --- learner payload --------------------------------------------------


def test_learner_payload_contains_prompt_and_options():
    payload = _provider().learner_payload(VALID_CONTENT)

    assert payload["type"] == "single_choice"
    assert payload["prompt"] == VALID_CONTENT["prompt"]
    assert payload["options"] == [{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}]


# --- answer-key leak regression (mandatory) --------------------------------


def test_learner_payload_never_leaks_the_answer_key():
    secret_content = {
        "prompt": "Which command lists files?",
        "options": [
            {"id": "a", "text": "ls"},
            {"id": "b", "text": "rm"},
        ],
    }
    secret_answer_key = {"correct_option_id": "SERVER_ONLY_CORRECT_OPTION_SECRET"}

    payload = _provider().learner_payload(secret_content)
    serialized = json.dumps(payload)

    assert "answer_key" not in payload
    assert "correct_option_id" not in payload
    assert "correct" not in payload
    assert "is_correct" not in payload
    assert set(payload.keys()) == {"type", "prompt", "options"}
    assert "SERVER_ONLY_CORRECT_OPTION_SECRET" not in serialized
    assert "answer_key" not in serialized
    assert "correct_option_id" not in serialized

    # sanity: the secret really was in the answer key we withheld
    assert secret_answer_key["correct_option_id"] == "SERVER_ONLY_CORRECT_OPTION_SECRET"


def test_evaluate_result_never_leaks_the_answer_key():
    content = {
        "prompt": "Pick one",
        "options": [
            {"id": "a", "text": "A"},
            {"id": "b", "text": "B"},
        ],
    }
    secret_answer_key = {"correct_option_id": "b"}

    result = _provider().evaluate(content, secret_answer_key, VALID_SCORING, {"selected_option_id": "a"})
    serialized = json.dumps(result)

    assert "correct_option_id" not in result
    assert "answer_key" not in result
    assert set(result.keys()) == {"correct", "score", "max_score"}
    assert "correct_option_id" not in serialized
    assert '"b"' not in serialized
