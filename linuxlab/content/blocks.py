import uuid

from django.core.exceptions import ValidationError
from wagtail import blocks


def validate_uuid(value):
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError("Enter a valid UUID.") from exc


class HeadingBlock(blocks.CharBlock):
    class Meta:
        icon = "title"
        label = "Heading"


class CommandBlock(blocks.StructBlock):
    """Documentation content describing a shell command. Never executed (ADR-005)."""

    command = blocks.CharBlock(help_text="The command shown to the learner.")
    expected_output = blocks.TextBlock(required=False)

    class Meta:
        icon = "code"
        label = "Command"


class TipBlock(blocks.StructBlock):
    heading = blocks.CharBlock(required=False)
    body = blocks.RichTextBlock()

    class Meta:
        icon = "help"
        label = "Tip"


class ActivityReferenceBlock(blocks.StructBlock):
    """Stable content reference to a future Task 05 ActivityDefinition/Version.

    Stores only the UUID + version pair; it must never resolve, embed
    "latest", or depend on a Task 05 model (ADR-004, ADR-009).
    """

    activity_public_id = blocks.CharBlock(validators=(validate_uuid,))
    activity_version = blocks.IntegerBlock(min_value=1)

    class Meta:
        icon = "link"
        label = "Activity reference"


class LessonStreamBlock(blocks.StreamBlock):
    heading = HeadingBlock()
    text = blocks.RichTextBlock()
    command = CommandBlock()
    tip = TipBlock()
    activity = ActivityReferenceBlock()
