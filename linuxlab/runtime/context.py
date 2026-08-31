"""Task 13 runtime package context: pin one exact ACTIVE InstalledPackage
per request/use-case, and rebind it to persisted identity on every
subsequent lookup without re-checking ACTIVE (T13-D01, T13-D02, T13-D08,
T13-D09, T13-D10, T13-D11).

Corrective (independent review, "seal RuntimePackageContext opening
provenance"): DB-identity equality alone does not prove a context was
ever legitimately minted -- a manually constructed RuntimePackageContext
carrying an ARCHIVED or STAGED package's exact persisted identity would
otherwise pass _rebind_runtime_context() undetected, even though
open_runtime_context() never issued it. A process-local HMAC seal closes
that gap: it is same-process API provenance (proof the context came from
open_runtime_context() in this process), never a defense against
arbitrary code already executing with unrestricted introspection inside
this same trusted process -- a legitimate sealed context, if copied,
remains a legitimate capability. It does not change T13-D11: after
provenance is verified, package.state is still never rechecked.
"""

import hashlib
import hmac
import json
import secrets

from linuxlab.sync.models import InstalledPackage
from linuxlab.sync.services import get_active_package

from .dto import RuntimePackageContext
from .exceptions import RuntimeContentUnavailable, RuntimeContextInvalid

# Process-local only: exists for the lifetime of this Python process,
# never persisted, never derived from Django SECRET_KEY, and unrelated to
# Task 11/12 package signing keys. A RuntimePackageContext is a request/
# use-case scoped capability that is not expected to survive a process
# restart, so there is nothing to migrate or rotate.
_CONTEXT_PROVENANCE_KEY = secrets.token_bytes(32)


def _canonical_context_identity_bytes(
    package_pk, package_type, source_public_id, package_version, payload_sha256
):
    """Deterministic, unambiguous canonical serialization of exactly the
    identity fields a RuntimePackageContext claims -- sorted keys, no
    delimiter that could make two distinct identity tuples collide."""
    return json.dumps(
        {
            "package_pk": package_pk,
            "package_type": package_type,
            "source_public_id": str(source_public_id),
            "package_version": package_version,
            "payload_sha256": payload_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _seal_runtime_context_identity(
    package_pk, package_type, source_public_id, package_version, payload_sha256
):
    canonical = _canonical_context_identity_bytes(
        package_pk, package_type, source_public_id, package_version, payload_sha256
    )
    return hmac.new(_CONTEXT_PROVENANCE_KEY, canonical, hashlib.sha256).digest()


def _has_valid_runtime_context_provenance(context):
    """Fail closed on any malformed input: an ordinary caller can
    construct RuntimePackageContext with a `_provenance_tag` that is not
    bytes at all (missing, None, str, ...), which must be treated as
    invalid rather than raising an uncaught TypeError out of
    hmac.compare_digest()."""
    actual = getattr(context, "_provenance_tag", None)
    if not isinstance(actual, bytes):
        return False

    expected = _seal_runtime_context_identity(
        context.package_pk,
        context.package_type,
        context.source_public_id,
        context.package_version,
        context.payload_sha256,
    )
    return hmac.compare_digest(actual, expected)


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


def open_pinned_context(package):
    """Mint a RuntimePackageContext for an InstalledPackage the caller has
    already legitimately resolved through its own trust boundary -- e.g.
    linuxlab.learning grading an existing Attempt via
    attempt.installed_activity_version.package (T15-D27 S7, S8).

    Unlike open_runtime_context(), this does NOT require package.state ==
    ACTIVE: an Attempt remains pinned to whatever package it started
    against even after a newer package activates or this one becomes
    ARCHIVED. Grading (or re-rendering) an EXISTING Attempt must never
    re-resolve "the current active package" -- that lookup is exactly
    what open_runtime_context() performs, and it is the wrong one here.
    """
    return _context_from_package(package)


def _context_from_package(package):
    provenance_tag = _seal_runtime_context_identity(
        package.pk,
        package.package_type,
        package.source_public_id,
        package.package_version,
        package.payload_sha256,
    )
    return RuntimePackageContext(
        package_pk=package.pk,
        package_type=package.package_type,
        source_public_id=package.source_public_id,
        package_version=package.package_version,
        payload_sha256=package.payload_sha256,
        _provenance_tag=provenance_tag,
    )


def _rebind_runtime_context(context):
    """The sole trust boundary for an already-opened RuntimePackageContext:
    a frozen dataclass is constructible by ordinary Python code, so it is
    never trusted on its own. Two independent checks, in order:

    1. provenance: the context's HMAC seal must verify against its own
       claimed identity fields -- proof it was minted by
       open_runtime_context() in this process, not merely a
       coincidentally-matching identity tuple assembled by hand;
    2. persisted identity: InstalledPackage reloaded by the context's
       local package_pk must still match every claimed identity field
       exactly.

    Either failing raises RuntimeContextInvalid. Deliberately does NOT
    require package.state == ACTIVE: a package that became ARCHIVED after
    this context was opened remains a valid pin for the rest of that
    request/use-case (T13-D11) -- Task 12 retains archived artifacts
    immutably, so there is nothing unsafe about continuing to read them.
    The provenance check proves legitimate issuance; it does not and
    should not encode anything about the package's current state.
    """
    if not _has_valid_runtime_context_provenance(context):
        raise RuntimeContextInvalid("RuntimePackageContext provenance is missing or invalid")

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
