import hashlib
import json

from django.db import transaction

from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.models import PublicationSnapshot
from linuxlab.publication.validators import validate_learning_path


def canonical_json_bytes(payload):
    """Deterministic canonical serialization for hashing: sorted keys,
    compact separators, UTF-8. The same semantic payload always produces
    the same bytes regardless of construction/dict-iteration order.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def compute_content_hash(payload):
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _serialize_block(block):
    if block.block_type == "heading":
        return {"type": "heading", "value": str(block.value)}
    if block.block_type == "text":
        return {"type": "text", "value": str(block.value)}
    if block.block_type == "command":
        return {
            "type": "command",
            "value": {
                "command": block.value["command"],
                "expected_output": block.value["expected_output"],
            },
        }
    if block.block_type == "tip":
        return {
            "type": "tip",
            "value": {
                "heading": block.value["heading"],
                "body": str(block.value["body"]),
            },
        }
    if block.block_type == "activity":
        return {
            "type": "activity",
            "value": {
                "activity_public_id": str(block.value["activity_public_id"]),
                "activity_version": block.value["activity_version"],
            },
        }
    raise ValueError(f"unknown block type '{block.block_type}'")


def _serialize_lesson(lesson):
    return {
        "public_id": str(lesson.public_id),
        "title": lesson.title,
        "blocks": [_serialize_block(block) for block in lesson.body],
    }


def _serialize_module(module):
    lessons = LessonPage.objects.child_of(module).specific().order_by("path")
    return {
        "public_id": str(module.public_id),
        "title": module.title,
        "lessons": [_serialize_lesson(lesson) for lesson in lessons],
    }


def build_learning_path_payload(learning_path_page):
    """Freeze structured semantic content only (ADR-003): block type, order,
    and semantic values, plus stable public identities/exact versions for
    every reference. Never local PKs, Wagtail revision IDs, or rendered
    HTML output.
    """
    modules = ModulePage.objects.child_of(learning_path_page).specific().order_by("path")
    return {
        "kind": "learning_path",
        "learning_path": {
            "public_id": str(learning_path_page.public_id),
            "title": learning_path_page.title,
            "modules": [_serialize_module(module) for module in modules],
        },
    }


def publish_learning_path(learning_path_page):
    """Validate dependencies, freeze a deterministic canonical snapshot, and
    allocate the next immutable publication version for this LearningPathPage
    (ADR-014).

    The payload is built and hashed OUTSIDE the lock (read-only walk of the
    current draft tree, READ COMMITTED); only version allocation itself is
    serialized, via a row lock on the LearningPathPage -- the same pattern
    Task 05 uses to lock ActivityDefinition before allocating the next
    ActivityVersion. Locking the whole descendant tree for full snapshot
    serializability would be over-engineering this task does not require.
    Fails closed: validate_learning_path() raises before anything is built
    or written if any dependency is missing/unpublished/malformed.
    """
    validate_learning_path(learning_path_page)
    payload = build_learning_path_payload(learning_path_page)
    content_hash = compute_content_hash(payload)

    with transaction.atomic():
        locked_page = LearningPathPage.objects.select_for_update().get(pk=learning_path_page.pk)

        current_max = (
            PublicationSnapshot.objects.filter(
                kind="learning_path", source_public_id=locked_page.public_id
            )
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
        )
        next_version = (current_max or 0) + 1

        return PublicationSnapshot.objects.create(
            kind="learning_path",
            source_public_id=locked_page.public_id,
            version=next_version,
            payload=payload,
            content_hash=content_hash,
        )
