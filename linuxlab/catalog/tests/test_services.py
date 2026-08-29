import uuid

import pytest

from linuxlab.catalog.models import Distribution, InstallationRecipe, Software
from linuxlab.catalog.services import get_install_command


@pytest.fixture
def libreoffice(db):
    return Software.objects.create(name="LibreOffice")


@pytest.fixture
def ubuntu(db):
    return Distribution.objects.create(name="Ubuntu")


@pytest.fixture
def fedora(db):
    return Distribution.objects.create(name="Fedora")


@pytest.mark.django_db
def test_one_libreoffice_catalog_record_returns_distro_specific_install_commands(
    libreoffice, ubuntu, fedora
):
    InstallationRecipe.objects.create(
        software=libreoffice, distribution=ubuntu, command="sudo apt install libreoffice"
    )
    InstallationRecipe.objects.create(
        software=libreoffice, distribution=fedora, command="sudo dnf install libreoffice"
    )

    ubuntu_command = get_install_command(libreoffice.public_id, ubuntu.public_id)
    fedora_command = get_install_command(libreoffice.public_id, fedora.public_id)

    assert ubuntu_command == "sudo apt install libreoffice"
    assert fedora_command == "sudo dnf install libreoffice"
    assert ubuntu_command != fedora_command


@pytest.mark.django_db
def test_lookup_is_by_stable_public_uuid_not_local_pk(libreoffice, ubuntu):
    InstallationRecipe.objects.create(
        software=libreoffice, distribution=ubuntu, command="sudo apt install libreoffice"
    )

    # Local PKs happen to be small integers in tests; passing one where a
    # UUID is expected must not silently resolve anything.
    with pytest.raises((InstallationRecipe.DoesNotExist, ValueError, TypeError)):
        get_install_command(libreoffice.pk, ubuntu.pk)


@pytest.mark.django_db
def test_missing_recipe_for_a_real_software_distribution_pair_raises_does_not_exist(
    libreoffice, ubuntu
):
    with pytest.raises(InstallationRecipe.DoesNotExist):
        get_install_command(libreoffice.public_id, ubuntu.public_id)


@pytest.mark.django_db
def test_unknown_software_or_distribution_uuid_raises_does_not_exist(libreoffice, ubuntu):
    InstallationRecipe.objects.create(
        software=libreoffice, distribution=ubuntu, command="sudo apt install libreoffice"
    )

    with pytest.raises(InstallationRecipe.DoesNotExist):
        get_install_command(uuid.uuid4(), ubuntu.public_id)

    with pytest.raises(InstallationRecipe.DoesNotExist):
        get_install_command(libreoffice.public_id, uuid.uuid4())
