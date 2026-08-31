"""T15-D27 Node execution-source resolution: the one runtime module that
returns a real Django model instance (InstalledActivityVersion) rather than
a frozen DTO. It exists specifically because linuxlab.learning.Attempt.
installed_activity_version is a genuine ForeignKey, not a learner-facing
payload -- content.py/grading.py's DTO-only contract is unrelated and
unchanged.
"""

from linuxlab.sync.models import InstalledActivityVersion

from .context import _rebind_runtime_context
from .exceptions import RuntimeArtifactNotFound


def get_installed_activity_for_attempt(context, activity_public_id, version):
    """Rebind `context` and return the actual InstalledActivityVersion row
    (not a DTO) scoped to the pinned package, for Attempt creation
    (linuxlab.learning.services.start_installed_attempt())."""
    package = _rebind_runtime_context(context)

    activity = InstalledActivityVersion.objects.filter(
        package=package,
        activity_public_id=activity_public_id,
        version=version,
    ).first()
    if activity is None:
        raise RuntimeArtifactNotFound(
            f"no activity {activity_public_id} v{version} in InstalledPackage {package.pk}"
        )

    return activity
