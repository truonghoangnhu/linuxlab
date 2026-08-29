from linuxlab.catalog.models import InstallationRecipe


def get_install_command(software_public_id, distribution_public_id):
    """Resolve the exact install command for a (Software, Distribution)
    pair by their stable public UUIDs -- never local primary keys -- so
    Lessons/callers reference catalog entities the same way they reference
    ActivityVersions (ADR-007, ADR-011): by stable identity, not by
    duplicating the command text.
    """
    return InstallationRecipe.objects.get(
        software__public_id=software_public_id,
        distribution__public_id=distribution_public_id,
    ).command
