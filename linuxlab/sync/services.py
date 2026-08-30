"""Node package lifecycle: verify -> stage -> activate -> rollback
(ADR-008, ADR-010, ADR-014, Task 12).

import_package() is the ONLY public trust entry point. It takes raw
`.llpkg` bytes and calls Task 11's verify_package() itself -- a caller can
never hand this module a pre-built VerifiedPackage instead, because a
manually constructed VerifiedPackage is not cryptographic proof of
anything (Python objects can always be constructed by hand). Only
verified.payload_bytes -- the exact bytes verify_package() cryptographically
checked -- is ever parsed and persisted, and that parse happens here, once,
inside the trusted staging path.

Staging is transactional and never activates anything: a newly staged
package is always `staged`, regardless of whether an older version of the
same source is currently `active`. Activation and rollback are separate,
explicit, atomically-locked lifecycle transitions that never touch
semantic artifact rows, never require Central or the Internet, and never
rebuild or re-verify anything -- they only flip InstalledPackage.state
(and activated_at/previous_package) under a row lock scoped to
(package_type, source_public_id).
"""

import json
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from linuxlab.publication.package import PackageVerificationError, verify_package

from .models import (
    ACTIVE,
    ARCHIVED,
    STAGED,
    InstalledActivityVersion,
    InstalledPackage,
    InstalledPublication,
    InstalledQuestionGrading,
    InstalledQuestionVersion,
)


class PackageImportError(Exception):
    pass


class PackageIdentityConflict(PackageImportError):
    pass


class PackageActivationError(Exception):
    pass


class PackageRollbackError(Exception):
    pass


def import_package(package_bytes, activity_registry, trusted_public_key):
    """Verify raw `.llpkg` bytes and stage them as a new, inert
    InstalledPackage (state=staged). Never activates anything.

    Node-capable-profile only: a deployment where
    settings.LINUXLAB_CAN_IMPORT_PACKAGES is falsy (e.g. Central) refuses
    outright, before Task 11 verification even runs.
    """
    if not getattr(settings, "LINUXLAB_CAN_IMPORT_PACKAGES", False):
        raise PackageImportError("package import is not permitted on this deployment profile")

    try:
        verified = verify_package(package_bytes, activity_registry, trusted_public_key)
    except PackageVerificationError as exc:
        raise PackageImportError(f"package verification failed: {exc}") from exc

    return _stage_verified_package(verified)


def _stage_verified_package(verified):
    """Internal only: parse and persist verified.payload_bytes -- the
    exact bytes Task 11 cryptographically checked. Never exported as an
    equivalent public trust entry point: accepting a caller-supplied
    VerifiedPackage here directly would let a manually constructed (i.e.
    unverified) instance reach the database.
    """
    manifest = verified.manifest
    payload = json.loads(verified.payload_bytes.decode("utf-8"))

    package_type = manifest.package_type
    source_public_id = manifest.package_uuid
    package_version = manifest.package_version
    payload_sha256 = manifest.payload_sha256

    with transaction.atomic():
        existing = (
            InstalledPackage.objects.select_for_update()
            .filter(
                package_type=package_type,
                source_public_id=source_public_id,
                package_version=package_version,
            )
            .first()
        )
        if existing is not None:
            if existing.payload_sha256 == payload_sha256:
                return existing
            raise PackageIdentityConflict(
                f"{package_type} {source_public_id} v{package_version} is already "
                "installed with a different payload hash"
            )

        package = InstalledPackage.objects.create(
            package_type=package_type,
            source_public_id=source_public_id,
            package_version=package_version,
            payload_sha256=payload_sha256,
            schema_version=manifest.schema_version,
            minimum_linuxlab_version=manifest.minimum_linuxlab_version,
            signature_algorithm=manifest.signature_algorithm,
            manifest_created_at=manifest.created_at,
            state=STAGED,
        )

        publication = payload["publication"]
        InstalledPublication.objects.create(
            package=package,
            kind=publication["kind"],
            source_public_id=uuid.UUID(publication["source_public_id"]),
            version=publication["version"],
            content_hash=publication["content_hash"],
            payload=publication["payload"],
        )

        for entry in payload["activities"]:
            InstalledActivityVersion.objects.create(
                package=package,
                activity_public_id=uuid.UUID(entry["activity_public_id"]),
                version=entry["version"],
                type_key=entry["type_key"],
                config=entry["config"],
            )

        question_rows = {}
        for entry in payload["questions"]:
            question_public_id = uuid.UUID(entry["question_public_id"])
            row = InstalledQuestionVersion.objects.create(
                package=package,
                question_public_id=question_public_id,
                version=entry["version"],
                type_key=entry["type_key"],
                content=entry["content"],
            )
            question_rows[(question_public_id, entry["version"])] = row

        for entry in payload["server"]["questions"]:
            question_public_id = uuid.UUID(entry["question_public_id"])
            question_row = question_rows[(question_public_id, entry["version"])]
            InstalledQuestionGrading.objects.create(
                question=question_row,
                answer_key=entry["answer_key"],
                scoring=entry["scoring"],
            )

    return package


def activate_package(package):
    """Atomically activate `package` (an InstalledPackage, staged or
    archived) within its own (package_type, source_public_id) scope.

    The currently active package in that scope, if any, becomes archived;
    `package` becomes active; both transitions happen in one transaction
    under a row lock covering every InstalledPackage row in that scope, so
    no concurrent activation of the same source can ever produce two
    simultaneously active packages. Activating a package that is already
    active is an idempotent no-op. Semantic artifact rows are never
    touched -- only lifecycle metadata changes.
    """
    with transaction.atomic():
        seed = (
            InstalledPackage.objects.filter(pk=package.pk)
            .values("package_type", "source_public_id")
            .first()
        )
        if seed is None:
            raise PackageActivationError(f"InstalledPackage {package.pk} does not exist")

        scope_rows = list(
            InstalledPackage.objects.select_for_update()
            .filter(
                package_type=seed["package_type"],
                source_public_id=seed["source_public_id"],
            )
            .order_by("pk")
        )
        candidate = next(row for row in scope_rows if row.pk == package.pk)

        if candidate.state == ACTIVE:
            return candidate

        if candidate.state not in (STAGED, ARCHIVED):
            raise PackageActivationError(f"cannot activate package in state {candidate.state!r}")

        current_active = next(
            (row for row in scope_rows if row.state == ACTIVE and row.pk != candidate.pk),
            None,
        )

        if current_active is not None:
            InstalledPackage.objects.filter(pk=current_active.pk).update(state=ARCHIVED)

        candidate.state = ACTIVE
        candidate.activated_at = timezone.now()
        candidate.previous_package = current_active
        candidate.save(update_fields=["state", "activated_at", "previous_package"])

    return candidate


def rollback_package(package_type, source_public_id):
    """Roll back the currently active package for
    (package_type, source_public_id) to its recorded previous_package.

    Never rebuilds, re-verifies, redownloads, or contacts Central: the
    previous package's staged artifacts are reused exactly as they were
    originally imported. Fails closed if there is no active package, no
    recorded previous package, or the recorded previous package does not
    belong to this exact source.
    """
    with transaction.atomic():
        scope_rows = list(
            InstalledPackage.objects.select_for_update()
            .filter(package_type=package_type, source_public_id=source_public_id)
            .order_by("pk")
        )

        current_active = next((row for row in scope_rows if row.state == ACTIVE), None)
        if current_active is None:
            raise PackageRollbackError(
                f"no active package for {package_type!r}/{source_public_id!s}"
            )

        if current_active.previous_package_id is None:
            raise PackageRollbackError("active package has no previous package to roll back to")

        previous = next(
            (row for row in scope_rows if row.pk == current_active.previous_package_id),
            None,
        )
        if (
            previous is None
            or previous.package_type != package_type
            or previous.source_public_id != source_public_id
        ):
            raise PackageRollbackError("previous package is not a valid local rollback target")

        InstalledPackage.objects.filter(pk=current_active.pk).update(state=ARCHIVED)

        previous.state = ACTIVE
        previous.activated_at = timezone.now()
        previous.save(update_fields=["state", "activated_at"])

    return previous


def get_active_package(package_type, source_public_id):
    """Return the exactly-one active InstalledPackage for this source, or
    None. Never a 'latest version' fallback: activation is an explicit
    lifecycle decision, not a version comparison.
    """
    return InstalledPackage.objects.filter(
        package_type=package_type,
        source_public_id=source_public_id,
        state=ACTIVE,
    ).first()
