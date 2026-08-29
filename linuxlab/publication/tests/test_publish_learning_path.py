import hashlib
import json
import threading

import pytest
from django.db import close_old_connections, connection

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.models import PublicationSnapshot
from linuxlab.publication.snapshots import canonical_json_bytes, publish_learning_path

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


def _simple_learning_path(title="Linux Basics"):
    path = LearningPathPage(title=title)
    _root().add_child(instance=path)
    return path


# --- version allocation -----------------------------------------------


@pytest.mark.django_db
def test_first_publish_is_version_one(registry):
    path = _simple_learning_path()

    snapshot = publish_learning_path(path, registry)

    assert snapshot.version == 1
    assert snapshot.kind == "learning_path"
    assert snapshot.source_public_id == path.public_id


@pytest.mark.django_db
def test_republish_creates_version_two(registry):
    path = _simple_learning_path()
    publish_learning_path(path, registry)

    second = publish_learning_path(path, registry)

    assert second.version == 2
    assert PublicationSnapshot.objects.filter(source_public_id=path.public_id).count() == 2


@pytest.mark.django_db
def test_different_learning_paths_each_start_at_version_one(registry):
    first = _simple_learning_path("Linux Basics")
    second = _simple_learning_path("Networking Basics")

    first_snapshot = publish_learning_path(first, registry)
    second_snapshot = publish_learning_path(second, registry)

    assert first_snapshot.version == 1
    assert second_snapshot.version == 1


# --- draft edit after publication does not mutate old snapshot --------


@pytest.mark.django_db
def test_editing_draft_after_publication_does_not_change_old_snapshot(registry):
    path = _simple_learning_path()
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [("heading", "Original heading")]
    module.add_child(instance=lesson)

    first = publish_learning_path(path, registry)
    original_payload = first.payload
    original_hash = first.content_hash

    lesson.body = [("heading", "Edited heading")]
    lesson.save_revision().publish()

    reloaded_first = PublicationSnapshot.objects.get(pk=first.pk)
    assert reloaded_first.payload == original_payload
    assert reloaded_first.content_hash == original_hash

    second = publish_learning_path(path, registry)
    assert second.version == 2
    assert second.payload != original_payload
    assert second.content_hash != original_hash

    # N still unchanged after N+1 exists
    reloaded_first_again = PublicationSnapshot.objects.get(pk=first.pk)
    assert reloaded_first_again.payload == original_payload


# --- exact ActivityVersion pinning survives newer versions -------------


@pytest.mark.django_db
def test_old_publication_still_references_original_activity_version_after_a_newer_one_publishes(
    definition, registry, published_version
):
    path = _simple_learning_path()
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(definition.public_id),
                "activity_version": published_version.version,
            },
        )
    ]
    module.add_child(instance=lesson)

    snapshot = publish_learning_path(path, registry)
    activity_ref = snapshot.payload["learning_path"]["modules"][0]["lessons"][0]["blocks"][0]["value"]
    assert activity_ref["activity_version"] == 1

    newer = create_activity_version(definition, config={})
    publish_activity_version(newer, registry)
    assert newer.version == 2

    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)
    reloaded_ref = reloaded.payload["learning_path"]["modules"][0]["lessons"][0]["blocks"][0]["value"]
    assert reloaded_ref["activity_version"] == 1


# --- failure: unpublished dependency blocks publication -----------------


@pytest.mark.django_db
def test_publish_rejected_when_activity_reference_unpublished(definition, registry):
    unpublished = create_activity_version(definition, config={})
    path = _simple_learning_path()
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(definition.public_id),
                "activity_version": unpublished.version,
            },
        )
    ]
    module.add_child(instance=lesson)

    with pytest.raises(ValueError):
        publish_learning_path(path, registry)

    assert PublicationSnapshot.objects.filter(source_public_id=path.public_id).count() == 0


@pytest.mark.django_db
def test_publish_succeeds_after_dependency_is_published(definition, registry):
    unpublished = create_activity_version(definition, config={})
    path = _simple_learning_path()
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(definition.public_id),
                "activity_version": unpublished.version,
            },
        )
    ]
    module.add_child(instance=lesson)

    with pytest.raises(ValueError):
        publish_learning_path(path, registry)

    publish_activity_version(unpublished, registry)

    snapshot = publish_learning_path(path, registry)
    assert snapshot.version == 1


# --- hash trust (section 10/34) -----------------------------------------


@pytest.mark.django_db
def test_persisted_hash_independently_matches_recomputed_sha256_of_persisted_payload(registry):
    path = _simple_learning_path()
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [("heading", "Hello")]
    module.add_child(instance=lesson)

    snapshot = publish_learning_path(path, registry)
    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)

    independently_computed = hashlib.sha256(canonical_json_bytes(reloaded.payload)).hexdigest()
    assert reloaded.content_hash == independently_computed


@pytest.mark.django_db
def test_republishing_unchanged_content_gets_a_new_version_but_the_same_hash(registry):
    path = _simple_learning_path()

    first = publish_learning_path(path, registry)
    second = publish_learning_path(path, registry)

    assert second.version == first.version + 1
    assert second.content_hash == first.content_hash
    assert second.payload == first.payload


# --- learner safety (section 27) ----------------------------------------


@pytest.mark.django_db
def test_persisted_payload_reloaded_from_db_contains_no_learner_unsafe_fields(registry):
    path = _simple_learning_path()
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [("heading", "SERVER_SECRET should never appear")]
    module.add_child(instance=lesson)

    snapshot = publish_learning_path(path, registry)
    reloaded = PublicationSnapshot.objects.get(pk=snapshot.pk)

    serialized = json.dumps(reloaded.payload)
    assert "answer_key" not in serialized
    assert "correct_option_id" not in serialized
    assert "scoring" not in serialized


# --- concurrency: two concurrent publishes of the same LearningPathPage --


class _PublishWorker(threading.Thread):
    def __init__(self, path_pk, registry, results, index):
        super().__init__()
        self.path_pk = path_pk
        self.registry = registry
        self.results = results
        self.index = index

    def run(self):
        close_old_connections()
        try:
            path = LearningPathPage.objects.get(pk=self.path_pk)
            snapshot = publish_learning_path(path, self.registry)
            self.results[self.index] = snapshot.version
        finally:
            connection.close()


@pytest.mark.django_db(transaction=True)
def test_concurrent_publications_allocate_distinct_sequential_versions():
    path = _simple_learning_path()
    registry = ActivityRegistry()
    registry.register(FakeProvider())

    results = [None, None]
    workers = [_PublishWorker(path.pk, registry, results, i) for i in range(2)]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sorted(results) == [1, 2]
    assert PublicationSnapshot.objects.filter(source_public_id=path.public_id).count() == 2
