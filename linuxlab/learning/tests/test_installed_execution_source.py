"""T15-D27: Attempt supports exactly one execution source -- a Central
ActivityVersion or a Node InstalledActivityVersion -- enforced by a
database CheckConstraint, never both and never neither."""

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.identity.models import Learner
from linuxlab.learning.models import Attempt
from linuxlab.learning.services import start_attempt


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
def learner(db):
    return Learner.objects.create(account=None)


@pytest.fixture
def published_version(definition, registry):
    version = create_activity_version(definition, config={})
    return publish_activity_version(version, registry)


@pytest.mark.django_db
def test_central_attempt_has_activity_version_and_no_installed_version(learner, published_version):
    attempt = start_attempt(learner, published_version)

    assert attempt.activity_version_id == published_version.pk
    assert attempt.installed_activity_version_id is None


@pytest.mark.django_db
def test_attempt_with_neither_execution_source_is_rejected(learner):
    with pytest.raises(IntegrityError), transaction.atomic():
        Attempt.objects.create(learner=learner, activity_version=None, installed_activity_version=None)


@pytest.mark.django_db(transaction=True)
@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_attempt_with_both_execution_sources_is_rejected(installed_active_world):
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    package_activity = world["package"].activities.get()

    with pytest.raises(IntegrityError), transaction.atomic():
        Attempt.objects.create(
            learner=learner,
            activity_version=world["activity_version"],
            installed_activity_version=package_activity,
        )


@pytest.mark.django_db(transaction=True)
@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_node_attempt_has_installed_version_and_no_activity_version(installed_active_world):
    world = installed_active_world
    learner = Learner.objects.create(account=None)
    package_activity = world["package"].activities.get()

    attempt = Attempt.objects.create(
        learner=learner, activity_version=None, installed_activity_version=package_activity
    )

    assert attempt.activity_version_id is None
    assert attempt.installed_activity_version_id == package_activity.pk


@pytest.mark.django_db(transaction=True)
@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_installed_activity_version_referenced_by_attempt_cannot_be_deleted(installed_active_world):
    from linuxlab.sync.models import InstalledActivityVersion

    world = installed_active_world
    learner = Learner.objects.create(account=None)
    package_activity = world["package"].activities.get()
    Attempt.objects.create(
        learner=learner, activity_version=None, installed_activity_version=package_activity
    )

    with pytest.raises(Exception):  # noqa: B017
        InstalledActivityVersion.objects.filter(pk=package_activity.pk).delete()

    assert InstalledActivityVersion.objects.filter(pk=package_activity.pk).exists()
