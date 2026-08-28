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


def _provider():
    return SingleChoiceProvider()


# --- validation: valid ------------------------------------------------


def test_valid_content_and_answer_key_pass_validation():
    _provider().validate(VALID_CONTENT, VALID_ANSWER_KEY)


def test_more_than_two_options_is_valid():
    content = {
        "prompt": "Pick one",
        "options": [
            {"id": "a", "text": "A"},
            {"id": "b", "text": "B"},
            {"id": "c", "text": "C"},
        ],
    }
    _provider().validate(content, {"correct_option_id": "c"})


# --- validation: invalid -----------------------------------------------


def test_empty_prompt_is_rejected():
    content = {**VALID_CONTENT, "prompt": ""}
    with pytest.raises(ValueError):
        _provider().validate(content, VALID_ANSWER_KEY)


def test_fewer_than_two_options_is_rejected():
    content = {"prompt": "Pick one", "options": [{"id": "a", "text": "A"}]}
    with pytest.raises(ValueError):
        _provider().validate(content, {"correct_option_id": "a"})


def test_duplicate_option_ids_are_rejected():
    content = {
        "prompt": "Pick one",
        "options": [
            {"id": "a", "text": "A"},
            {"id": "a", "text": "A again"},
        ],
    }
    with pytest.raises(ValueError):
        _provider().validate(content, {"correct_option_id": "a"})


def test_missing_option_id_is_rejected():
    content = {
        "prompt": "Pick one",
        "options": [
            {"text": "A"},
            {"id": "b", "text": "B"},
        ],
    }
    with pytest.raises(ValueError):
        _provider().validate(content, {"correct_option_id": "b"})


def test_missing_option_text_is_rejected():
    content = {
        "prompt": "Pick one",
        "options": [
            {"id": "a"},
            {"id": "b", "text": "B"},
        ],
    }
    with pytest.raises(ValueError):
        _provider().validate(content, {"correct_option_id": "b"})


def test_answer_referencing_nonexistent_option_is_rejected():
    with pytest.raises(ValueError):
        _provider().validate(VALID_CONTENT, {"correct_option_id": "does-not-exist"})


# --- evaluation -----------------------------------------------------------


def test_evaluate_correct_response():
    result = _provider().evaluate(VALID_CONTENT, VALID_ANSWER_KEY, {"selected_option_id": "a"})

    assert result["correct"] is True
    assert result["score"] == 1
    assert result["max_score"] == 1


def test_evaluate_incorrect_response():
    result = _provider().evaluate(VALID_CONTENT, VALID_ANSWER_KEY, {"selected_option_id": "b"})

    assert result["correct"] is False
    assert result["score"] == 0
    assert result["max_score"] == 1


def test_evaluate_malformed_response_is_handled_deterministically():
    result_none = _provider().evaluate(VALID_CONTENT, VALID_ANSWER_KEY, None)
    result_empty = _provider().evaluate(VALID_CONTENT, VALID_ANSWER_KEY, {})
    result_unknown_key = _provider().evaluate(
        VALID_CONTENT, VALID_ANSWER_KEY, {"unexpected": "x"}
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

    result = _provider().evaluate(content, secret_answer_key, {"selected_option_id": "a"})
    serialized = json.dumps(result)

    assert "correct_option_id" not in result
    assert "answer_key" not in result
    assert set(result.keys()) == {"correct", "score", "max_score"}
    assert "correct_option_id" not in serialized
    assert '"b"' not in serialized
