from typing import ClassVar

from django.db import models

from linuxlab.core.models import StableIdentityModel


class Software(StableIdentityModel):
    """A reusable, catalog-wide identity for one piece of software (e.g.
    "LibreOffice") that Lessons can reference by stable UUID instead of
    duplicating install metadata per lesson (ADR-007, ADR-011). Ordinary
    mutable reference data -- unlike Task 05-09's versioned models, nothing
    here is ever pinned to an exact historical state, so there is no
    save()/version invariant to protect.
    """

    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class Distribution(StableIdentityModel):
    """A reusable, catalog-wide identity for one Linux distribution (e.g.
    "Ubuntu"). Distribution versions (e.g. "22.04") are out of scope for
    this catalog -- Task 10 does not require pinning install instructions
    to an exact distro release, so none is modeled (YAGNI).
    """

    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class InstallationRecipe(models.Model):
    """The exact install command for one (Software, Distribution) pair.

    Purely reference/documentation data (ADR-005 lesson: Command content is
    never executed) -- `command` is stored and displayed as text and must
    never be passed to a shell. This model has no public_id of its own:
    nothing outside catalog ever needs to reference "recipe #N" directly,
    only the (software, distribution) pair it belongs to, via
    get_install_command().
    """

    software = models.ForeignKey(Software, on_delete=models.CASCADE, related_name="recipes")
    distribution = models.ForeignKey(
        Distribution, on_delete=models.CASCADE, related_name="recipes"
    )
    command = models.TextField()

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["software", "distribution"],
                name="unique_installation_recipe_software_distribution",
            ),
        ]

    def __str__(self):
        return f"{self.software} on {self.distribution}"
