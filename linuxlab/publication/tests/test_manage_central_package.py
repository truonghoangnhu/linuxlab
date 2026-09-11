import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.models import PublicationSnapshot

from ._wagtail_bootstrap import get_or_create_root_page


def _make_learning_path(title="Intro to Bash"):
    root = get_or_create_root_page()
    path = LearningPathPage(title=title, slug=title.lower().replace(" ", "-"))
    root.add_child(instance=path)
    return path


@pytest.mark.django_db(transaction=True)
def test_publish_capability_guard_precedes_uuid_validation(settings):
    settings.LINUXLAB_CAN_AUTHOR = False

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_central_package", "publish", "--learning-path", "not-a-uuid")

    message = str(excinfo.value)
    assert "not permitted on this deployment profile" in message
    assert "not a valid UUID" not in message
    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_publish_rejects_malformed_uuid(settings):
    settings.LINUXLAB_CAN_AUTHOR = True

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_central_package", "publish", "--learning-path", "not-a-uuid")

    message = str(excinfo.value)
    assert "not a valid UUID" in message
    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_publish_rejects_unknown_learning_path(settings):
    settings.LINUXLAB_CAN_AUTHOR = True
    unknown_id = uuid.uuid4()

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_central_package", "publish", "--learning-path", str(unknown_id))

    message = str(excinfo.value)
    assert str(unknown_id) in message
    assert "LearningPathPage object" not in message
    assert PublicationSnapshot.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_publish_reports_kind_learning_path_and_version(settings, capsys):
    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path()

    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

    snapshot = PublicationSnapshot.objects.get(source_public_id=path.public_id)
    assert snapshot.kind == "learning_path"
    assert snapshot.source_public_id == path.public_id
    assert snapshot.version == 1

    out = capsys.readouterr().out
    assert out == (
        "Operation: publish\n"
        "Kind: learning_path\n"
        f"LearningPath: {path.public_id}\n"
        "Version: 1\n"
    )
    assert "Publication:" not in out


@pytest.mark.django_db(transaction=True)
def test_publish_translates_existing_domain_rejection_for_unresolved_activity(settings):
    # Real, already-existing domain rejection (linuxlab/publication/
    # validators.py::_validate_activity_reference): an activity block
    # referencing an activity_public_id with no published ActivityVersion
    # raises ValueError from inside publish_learning_path() itself. This
    # is not a new CLI-invented failure mode -- it is the same rejection
    # linuxlab/publication/tests/test_publish_learning_path.py's
    # test_publish_rejected_when_activity_reference_unpublished already
    # exercises directly against the service.
    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path(title="Broken Path")
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(uuid.uuid4()),
                "activity_version": 1,
            },
        )
    ]
    module.add_child(instance=lesson)

    with pytest.raises(CommandError) as excinfo:
        call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

    assert "Traceback" not in str(excinfo.value)
    assert PublicationSnapshot.objects.filter(source_public_id=path.public_id).count() == 0


@pytest.mark.django_db(transaction=True)
def test_publish_rejects_wrong_profile_before_mutation_with_valid_target(settings):
    settings.LINUXLAB_CAN_AUTHOR = False
    path = _make_learning_path()

    with pytest.raises(CommandError):
        call_command(
            "manage_central_package", "publish", "--learning-path", str(path.public_id)
        )

    assert not PublicationSnapshot.objects.filter(source_public_id=path.public_id).exists()


@pytest.mark.django_db(transaction=True)
def test_publish_second_call_allocates_next_version(settings, capsys):
    settings.LINUXLAB_CAN_AUTHOR = True
    path = _make_learning_path()

    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))
    capsys.readouterr()
    call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

    out = capsys.readouterr().out
    assert "Version: 2" in out
