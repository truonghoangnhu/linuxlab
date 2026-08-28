import uuid

import pytest
from django.core.exceptions import ValidationError

from linuxlab.content.blocks import ActivityReferenceBlock, LessonStreamBlock


def test_lesson_stream_block_declares_expected_block_names():
    block_names = set(LessonStreamBlock().child_blocks.keys())

    assert block_names == {"heading", "text", "command", "tip", "activity"}


def test_activity_reference_block_accepts_valid_uuid_and_positive_version():
    block = ActivityReferenceBlock()
    value = block.to_python(
        {"activity_public_id": str(uuid.uuid4()), "activity_version": 3}
    )

    block.clean(value)


def test_activity_reference_block_rejects_version_below_one():
    block = ActivityReferenceBlock()
    value = block.to_python(
        {"activity_public_id": str(uuid.uuid4()), "activity_version": 0}
    )

    with pytest.raises(ValidationError):
        block.clean(value)


def test_activity_reference_block_rejects_invalid_uuid():
    block = ActivityReferenceBlock()
    value = block.to_python(
        {"activity_public_id": "not-a-uuid", "activity_version": 1}
    )

    with pytest.raises(ValidationError):
        block.clean(value)


def test_activity_reference_block_round_trip_serialization():
    activity_public_id = str(uuid.uuid4())
    block = ActivityReferenceBlock()

    native_value = block.to_python(
        {"activity_public_id": activity_public_id, "activity_version": 3}
    )
    cleaned = block.clean(native_value)

    prepped = block.get_prep_value(cleaned)
    restored = block.to_python(prepped)

    assert restored["activity_public_id"] == activity_public_id
    assert restored["activity_version"] == 3
    assert set(prepped.keys()) == {"activity_public_id", "activity_version"}
