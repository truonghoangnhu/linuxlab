import pytest

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.snapshots import (
    canonical_json_bytes,
    capture_learning_path,
    compute_content_hash,
)

from ._wagtail_bootstrap import get_or_create_root_page


class FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": 1, "max_score": 1}


@pytest.fixture
def registry():
    reg = ActivityRegistry()
    reg.register(FakeProvider())
    return reg


@pytest.fixture
def definition(db):
    return ActivityDefinition.objects.create(name="Fake activity", type_key="fake")


@pytest.fixture
def published_version(definition, registry):
    version = create_activity_version(definition, config={})
    return publish_activity_version(version, registry)


def _root():
    return get_or_create_root_page()


@pytest.mark.django_db
def test_canonical_json_bytes_uses_sorted_keys_and_compact_separators():
    payload = {"b": 1, "a": 2}

    assert canonical_json_bytes(payload) == b'{"a":2,"b":1}'


@pytest.mark.django_db
def test_same_payload_produces_same_hash():
    payload = {"a": 1, "b": [1, 2, 3]}

    assert compute_content_hash(payload) == compute_content_hash({"b": [1, 2, 3], "a": 1})


@pytest.mark.django_db
def test_different_payload_produces_different_hash():
    first = compute_content_hash({"a": 1})
    second = compute_content_hash({"a": 2})

    assert first != second


@pytest.mark.django_db
def test_content_hash_is_a_64_character_hex_sha256_digest():
    digest = compute_content_hash({"a": 1})

    assert len(digest) == 64
    int(digest, 16)  # must be valid hex


@pytest.mark.django_db
def test_capture_learning_path_preserves_structure_and_order(published_version):
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [
        ("heading", "Intro"),
        (
            "activity",
            {
                "activity_public_id": str(published_version.definition.public_id),
                "activity_version": published_version.version,
            },
        ),
    ]
    module.add_child(instance=lesson)

    capture = capture_learning_path(path)

    assert capture["kind"] == "learning_path"
    lp = capture["learning_path"]
    assert lp["public_id"] == str(path.public_id)
    assert lp["title"] == "Linux Basics"
    assert lp["modules"][0]["public_id"] == str(module.public_id)
    lesson_payload = lp["modules"][0]["lessons"][0]
    assert lesson_payload["public_id"] == str(lesson.public_id)
    assert lesson_payload["blocks"][0] == {"type": "heading", "value": "Intro"}
    assert lesson_payload["blocks"][1] == {
        "type": "activity",
        "value": {
            "activity_public_id": str(published_version.definition.public_id),
            "activity_version": published_version.version,
        },
    }


@pytest.mark.django_db
def test_capture_excludes_local_pk_and_wagtail_internals():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)

    capture = capture_learning_path(path)

    lp = capture["learning_path"]
    assert set(lp.keys()) == {"public_id", "title", "modules"}
    module_payload = lp["modules"][0]
    assert set(module_payload.keys()) == {"public_id", "title", "lessons"}


@pytest.mark.django_db
def test_capture_does_not_trust_a_stale_caller_object():
    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)

    stale = LearningPathPage.objects.get(pk=path.pk)
    LearningPathPage.objects.filter(pk=path.pk).update(title="Renamed Live")
    # `stale` still has the old in-memory title, but capture must resolve
    # the authoritative row, not trust stale caller state (ADR-014
    # corrective, section 15).

    capture = capture_learning_path(stale)

    assert capture["learning_path"]["title"] == "Renamed Live"
