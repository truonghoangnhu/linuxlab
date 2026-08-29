import uuid

import pytest
from django.db import IntegrityError, transaction

from linuxlab.catalog.models import Distribution, InstallationRecipe, Software


@pytest.fixture
def software(db):
    return Software.objects.create(name="LibreOffice")


@pytest.fixture
def ubuntu(db):
    return Distribution.objects.create(name="Ubuntu")


@pytest.fixture
def fedora(db):
    return Distribution.objects.create(name="Fedora")


# --- stable identity --------------------------------------------------


@pytest.mark.django_db
def test_software_receives_a_stable_uuid_public_id(software):
    assert isinstance(software.public_id, uuid.UUID)


@pytest.mark.django_db
def test_distribution_receives_a_stable_uuid_public_id(ubuntu):
    assert isinstance(ubuntu.public_id, uuid.UUID)


@pytest.mark.django_db
def test_two_software_records_never_share_a_public_id():
    first = Software.objects.create(name="LibreOffice")
    second = Software.objects.create(name="GIMP")

    assert first.public_id != second.public_id


# --- InstallationRecipe: the one required uniqueness -------------------


@pytest.mark.django_db
def test_installation_recipe_can_be_created_for_a_software_distribution_pair(software, ubuntu):
    recipe = InstallationRecipe.objects.create(
        software=software, distribution=ubuntu, command="sudo apt install libreoffice"
    )

    assert recipe.software_id == software.pk
    assert recipe.distribution_id == ubuntu.pk
    assert recipe.command == "sudo apt install libreoffice"


@pytest.mark.django_db
def test_duplicate_software_distribution_recipe_is_rejected(software, ubuntu):
    InstallationRecipe.objects.create(
        software=software, distribution=ubuntu, command="sudo apt install libreoffice"
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        InstallationRecipe.objects.create(
            software=software, distribution=ubuntu, command="sudo apt install libreoffice --force"
        )


@pytest.mark.django_db
def test_same_software_can_have_recipes_for_different_distributions(software, ubuntu, fedora):
    InstallationRecipe.objects.create(
        software=software, distribution=ubuntu, command="sudo apt install libreoffice"
    )
    InstallationRecipe.objects.create(
        software=software, distribution=fedora, command="sudo dnf install libreoffice"
    )

    assert InstallationRecipe.objects.filter(software=software).count() == 2


@pytest.mark.django_db
def test_same_distribution_can_serve_recipes_for_different_software(ubuntu):
    libreoffice = Software.objects.create(name="LibreOffice")
    gimp = Software.objects.create(name="GIMP")

    InstallationRecipe.objects.create(
        software=libreoffice, distribution=ubuntu, command="sudo apt install libreoffice"
    )
    InstallationRecipe.objects.create(
        software=gimp, distribution=ubuntu, command="sudo apt install gimp"
    )

    assert InstallationRecipe.objects.filter(distribution=ubuntu).count() == 2


# --- deletion semantics: internal to catalog only -----------------------


@pytest.mark.django_db
def test_deleting_software_removes_its_own_installation_recipes(software, ubuntu):
    InstallationRecipe.objects.create(
        software=software, distribution=ubuntu, command="sudo apt install libreoffice"
    )

    software.delete()

    assert InstallationRecipe.objects.count() == 0


@pytest.mark.django_db
def test_deleting_distribution_removes_its_own_installation_recipes(software, ubuntu):
    InstallationRecipe.objects.create(
        software=software, distribution=ubuntu, command="sudo apt install libreoffice"
    )

    ubuntu.delete()

    assert InstallationRecipe.objects.count() == 0


# --- ordinary mutability: catalog is reference data, not immutable -----


@pytest.mark.django_db
def test_software_name_can_be_edited_ordinarily(software):
    software.name = "LibreOffice Suite"
    software.save()

    reloaded = Software.objects.get(pk=software.pk)
    assert reloaded.name == "LibreOffice Suite"


@pytest.mark.django_db
def test_installation_recipe_command_can_be_edited_ordinarily(software, ubuntu):
    recipe = InstallationRecipe.objects.create(
        software=software, distribution=ubuntu, command="sudo apt install libreoffice"
    )

    recipe.command = "sudo apt install --yes libreoffice"
    recipe.save()

    reloaded = InstallationRecipe.objects.get(pk=recipe.pk)
    assert reloaded.command == "sudo apt install --yes libreoffice"
