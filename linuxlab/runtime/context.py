"""Task 13 runtime package context: pin one exact ACTIVE InstalledPackage
per request/use-case, and rebind it to persisted identity on every
subsequent lookup without re-checking ACTIVE (T13-D01, T13-D02, T13-D08,
T13-D09, T13-D10, T13-D11).
"""

from linuxlab.sync.models import InstalledPackage
from linuxlab.sync.services import get_active_package

from .dto import RuntimePackageContext
from .exceptions import RuntimeContentUnavailable, RuntimeContextInvalid


def open_runtime_context(package_type, source_public_id):
    """Pin the exactly-one ACTIVE InstalledPackage for
    (package_type, source_public_id) as an immutable RuntimePackageContext.

    Reuses Task 12's own get_active_package() lookup rather than
    reimplementing it -- the DB-level one-ACTIVE-per-source invariant
    makes this unambiguous. Never infers a highest/latest version: a
    STAGED or ARCHIVED package for this source, however recent, does not
    satisfy this call. Performs no long-running transaction; this is a
    single short read.
    """
    package = get_active_package(package_type, source_public_id)
    if package is None:
        raise RuntimeContentUnavailable(
            f"no ACTIVE package for {package_type!r}/{source_public_id!s}"
        )

    return _context_from_package(package)


def _context_from_package(package):
    return RuntimePackageContext(
        package_pk=package.pk,
        package_type=package.package_type,
        source_public_id=package.source_public_id,
        package_version=package.package_version,
        payload_sha256=package.payload_sha256,
    )


def _rebind_runtime_context(context):
    """The sole trust boundary for an already-opened RuntimePackageContext:
    a frozen dataclass is constructible by ordinary Python code, so it is
    never trusted on its own. Reload InstalledPackage by the context's
    local package_pk and require every persisted identity field to match
    the context's claim exactly; on any mismatch, or if the row no longer
    exists at all, raise RuntimeContextInvalid.

    Deliberately does NOT require package.state == ACTIVE: a package that
    became ARCHIVED after this context was opened remains a valid pin for
    the rest of that request/use-case (T13-D11) -- Task 12 retains
    archived artifacts immutably, so there is nothing unsafe about
    continuing to read them.
    """
    package = InstalledPackage.objects.filter(pk=context.package_pk).first()
    if package is None:
        raise RuntimeContextInvalid(f"InstalledPackage {context.package_pk} no longer exists")

    if (
        package.package_type != context.package_type
        or package.source_public_id != context.source_public_id
        or package.package_version != context.package_version
        or package.payload_sha256 != context.payload_sha256
    ):
        raise RuntimeContextInvalid(
            f"InstalledPackage {context.package_pk} identity does not match "
            "the pinned context claim"
        )

    return package
