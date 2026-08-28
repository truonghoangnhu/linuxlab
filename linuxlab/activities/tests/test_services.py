import threading
import uuid

import pytest
from django.db import close_old_connections, connection

from linuxlab.activities.models import ActivityDefinition, ActivityVersion
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import (
    create_activity_version,
    get_activity_version,
    publish_activity_version,
)


class FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        if "expected" not in config:
            raise ValueError("config must contain 'expected'")

    def evaluate(self, config, submission):
        return {"correct": submission == config["expected"]}


@pytest.fixture
def registry():
    reg = ActivityRegistry()
    reg.register(FakeProvider())
    return reg


@pytest.fixture
def definition(db):
    return ActivityDefinition.objects.create(name="Fake activity", type_key="fake")


# --- Version creation -------------------------------------------------


@pytest.mark.django_db
def test_first_created_version_is_one(definition):
    version = create_activity_version(definition)

    assert version.version == 1


@pytest.mark.django_db
def test_next_created_version_is_two(definition):
    create_activity_version(definition)
    version = create_activity_version(definition)

    assert version.version == 2


@pytest.mark.django_db
def test_third_created_version_is_three(definition):
    create_activity_version(definition)
    create_activity_version(definition)
    version = create_activity_version(definition)

    assert version.version == 3


# --- Publication --------------------------------------------------------


@pytest.mark.django_db
def test_valid_provider_config_publishes(definition, registry):
    version = create_activity_version(definition, config={"expected": "ls"})

    published = publish_activity_version(version, registry)

    assert published.published_at is not None
    assert published.is_published is True


@pytest.mark.django_db
def test_invalid_provider_config_does_not_publish(definition, registry):
    version = create_activity_version(definition, config={})

    with pytest.raises(ValueError):
        publish_activity_version(version, registry)

    reloaded = ActivityVersion.objects.get(pk=version.pk)
    assert reloaded.published_at is None


@pytest.mark.django_db
def test_already_published_version_cannot_be_republished(definition, registry):
    version = create_activity_version(definition, config={"expected": "ls"})
    publish_activity_version(version, registry)

    with pytest.raises(ValueError):
        publish_activity_version(version, registry)


@pytest.mark.django_db
def test_changing_content_requires_a_new_version(definition, registry):
    version = create_activity_version(definition, config={"expected": "ls"})
    publish_activity_version(version, registry)

    next_version = create_activity_version(definition, config={"expected": "pwd"})

    assert next_version.version == 2
    assert next_version.published_at is None


# --- Published immutability ---------------------------------------------


@pytest.mark.django_db
def test_published_config_cannot_be_changed_through_normal_save(definition, registry):
    version = create_activity_version(definition, config={"expected": "ls"})
    publish_activity_version(version, registry)

    version.config = {"expected": "changed"}
    with pytest.raises(ValueError):
        version.save()

    reloaded = ActivityVersion.objects.get(pk=version.pk)
    assert reloaded.config == {"expected": "ls"}


@pytest.mark.django_db
def test_published_required_capabilities_cannot_be_changed(definition, registry):
    version = create_activity_version(
        definition, config={"expected": "ls"}, required_capabilities=["shell"]
    )
    publish_activity_version(version, registry)

    version.required_capabilities = ["python3"]
    with pytest.raises(ValueError):
        version.save()

    reloaded = ActivityVersion.objects.get(pk=version.pk)
    assert reloaded.required_capabilities == ["shell"]


@pytest.mark.django_db
def test_published_version_number_cannot_be_silently_changed(definition, registry):
    version = create_activity_version(definition, config={"expected": "ls"})
    publish_activity_version(version, registry)

    version.version = 999
    with pytest.raises(ValueError):
        version.save()

    reloaded = ActivityVersion.objects.get(pk=version.pk)
    assert reloaded.version == 1


# --- Lookup ---------------------------------------------------------------


@pytest.mark.django_db
def test_get_activity_version_finds_published_exact_version(definition, registry):
    version = create_activity_version(definition, config={"expected": "ls"})
    publish_activity_version(version, registry)

    found = get_activity_version(definition.public_id, 1)

    assert found.pk == version.pk


@pytest.mark.django_db
def test_get_activity_version_rejects_wrong_version(definition, registry):
    version = create_activity_version(definition, config={"expected": "ls"})
    publish_activity_version(version, registry)

    with pytest.raises(ActivityVersion.DoesNotExist):
        get_activity_version(definition.public_id, 2)


@pytest.mark.django_db
def test_get_activity_version_rejects_wrong_definition_uuid(definition, registry):
    version = create_activity_version(definition, config={"expected": "ls"})
    publish_activity_version(version, registry)

    with pytest.raises(ActivityVersion.DoesNotExist):
        get_activity_version(uuid.uuid4(), 1)


@pytest.mark.django_db
def test_get_activity_version_does_not_return_unpublished_version(definition):
    create_activity_version(definition, config={"expected": "ls"})

    with pytest.raises(ActivityVersion.DoesNotExist):
        get_activity_version(definition.public_id, 1)


# --- Concurrent publication ----------------------------------------------


class _PublishWorker(threading.Thread):
    """Publishes the given version on its own DB connection, synchronized by
    barrier so both workers enter publish_activity_version() together."""

    def __init__(self, version_pk, registry, barrier, results, index):
        super().__init__()
        self.version_pk = version_pk
        self.registry = registry
        self.barrier = barrier
        self.results = results
        self.index = index

    def run(self):
        close_old_connections()
        try:
            version = ActivityVersion.objects.get(pk=self.version_pk)
            self.barrier.wait()
            try:
                publish_activity_version(version, self.registry)
                self.results[self.index] = "success"
            except ValueError:
                self.results[self.index] = "rejected"
        finally:
            connection.close()


@pytest.mark.django_db(transaction=True)
def test_concurrent_publication_results_in_exactly_one_success():
    definition = ActivityDefinition.objects.create(name="Fake activity", type_key="fake")
    version = create_activity_version(definition, config={"expected": "ls"})

    registry = ActivityRegistry()
    registry.register(FakeProvider())

    barrier = threading.Barrier(2)
    results = [None, None]
    workers = [_PublishWorker(version.pk, registry, barrier, results, i) for i in range(2)]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert results.count("success") == 1
    assert results.count("rejected") == 1

    reloaded = ActivityVersion.objects.get(pk=version.pk)
    assert reloaded.is_published is True
    assert ActivityVersion.objects.filter(definition=definition).count() == 1
