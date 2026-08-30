"""Task 13 runtime domain exceptions (T13-D13).

Plain Python exceptions, deliberately unaware of HTTP: the HTTP adapter
(linuxlab/sync/views.py) maps these to a response, never the other way
around, and the runtime core has no dependency on Django's request/
response machinery at all.
"""


class RuntimeContentUnavailable(Exception):
    """No exact ACTIVE InstalledPackage exists for the requested
    (package_type, source_public_id). Never raised as a substitute for
    "no staged/archived match either" -- staged/archived packages simply
    do not count (T13-D01, T13-D02)."""


class RuntimeArtifactNotFound(Exception):
    """The pinned package does not contain the exact requested
    publication/activity/question/grading artifact (exact UUID + version,
    scoped to that one package)."""


class RuntimeContextInvalid(Exception):
    """A RuntimePackageContext's claimed identity does not match the
    persisted InstalledPackage reloaded by its local package_pk, or that
    package_pk no longer resolves to any row at all. Never raised merely
    because the pinned package is now ARCHIVED (T13-D11)."""
