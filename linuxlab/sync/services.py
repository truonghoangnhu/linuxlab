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

Task 12 corrective (security/architecture review): public ORM is not an
alternative package-import mechanism (linuxlab/sync/models.py rejects
ordinary create/save/bulk_create/update/bulk_update/delete outright for
InstalledPackage and every staged artifact model). The only way any row in
this app is ever written is through the private trusted helpers below --
_insert_trusted() (bypasses save()'s rejection via Model.save_base(),
Task 09's _insert_published_snapshot() pattern) and
_write_installed_package_lifecycle() (bypasses QuerySet.update()'s
rejection by calling Django's base QuerySet.update() directly) -- both of
which are called ONLY from this module, strictly after Task 11 verification
(for insertion) or from inside a locked activate_package()/rollback_package()
transaction (for lifecycle writes).

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
from contextlib import contextmanager

from django.conf import settings
from django.db import IntegrityError, models, transaction
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

_LIFECYCLE_FIELDS = frozenset({"state", "activated_at", "previous_package"})


class PackageImportError(Exception):
    pass


class PackageIdentityConflict(PackageImportError):
    pass


class PackageActivationError(Exception):
    pass


class PackageRollbackError(Exception):
    pass


class InstalledActivityNotFoundError(Exception):
    """No InstalledActivityVersion exists for the given package identity
    scope (T15-D27 corrective). Raised only by
    lock_active_installed_activity_for_execution(); callers needing a
    runtime-domain exception (e.g. linuxlab.learning, which already
    depends on linuxlab.runtime) translate this at their own boundary --
    this module never imports linuxlab.runtime itself (that would be
    circular: linuxlab.runtime already depends on linuxlab.sync)."""


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


def _insert_trusted(model_cls, **fields):
    """The sole controlled insertion path for InstalledPackage and every
    immutable staged artifact model (Task 09's _insert_published_snapshot()
    pattern, linuxlab/publication/snapshots.py): bypasses their
    unconditional save() rejection by calling Django's underlying
    Model.save_base() directly, the same call save() itself would make had
    it not been overridden. `model_cls` is never accepted from a caller --
    every call site in this module hardcodes one of the five sync models.
    """
    instance = model_cls(**fields)
    models.Model.save_base(instance, force_insert=True)
    return instance


def _find_installed_package(package_type, source_public_id, package_version):
    """Locked existence lookup for one package identity, factored out so
    the concurrent-import recovery path (below) can re-run it outside a
    failed insertion attempt's savepoint."""
    return (
        InstalledPackage.objects.select_for_update()
        .filter(
            package_type=package_type,
            source_public_id=source_public_id,
            package_version=package_version,
        )
        .first()
    )


def _write_installed_package_lifecycle(package_pk, **fields):
    """The sole controlled mutation path for InstalledPackage lifecycle
    metadata (state/activated_at/previous_package only -- an explicit
    allow-list, never arbitrary **kwargs passed through). Bypasses
    InstalledPackageQuerySet.update()'s rejection by calling Django's
    underlying QuerySet.update() directly on a plain pk-filtered queryset,
    the same bypass spirit as _insert_trusted() above. Only ever called
    from activate_package()/rollback_package(), each already holding a
    select_for_update() lock on this row inside its own transaction --
    this function performs no locking of its own.
    """
    unexpected = set(fields) - _LIFECYCLE_FIELDS
    if unexpected:
        raise ValueError(
            f"cannot write non-lifecycle InstalledPackage fields: {sorted(unexpected)}"
        )

    updated = models.QuerySet.update(InstalledPackage.objects.filter(pk=package_pk), **fields)
    if updated != 1:
        raise ValueError(f"InstalledPackage {package_pk} does not exist")


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
        existing = _find_installed_package(package_type, source_public_id, package_version)
        if existing is not None:
            if existing.payload_sha256 == payload_sha256:
                return existing
            raise PackageIdentityConflict(
                f"{package_type} {source_public_id} v{package_version} is already "
                "installed with a different payload hash"
            )

        try:
            # A nested atomic() is a real PostgreSQL savepoint: if a
            # concurrent transaction commits the exact same new identity
            # between our existence check above and our own insert below,
            # the insert below hits the unique constraint and raises
            # IntegrityError, but only THIS savepoint rolls back -- the
            # outer transaction (and the existence re-check in the
            # `except` clause) is unaffected.
            with transaction.atomic():
                package = _insert_trusted(
                    InstalledPackage,
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
                _insert_trusted(
                    InstalledPublication,
                    package=package,
                    kind=publication["kind"],
                    source_public_id=uuid.UUID(publication["source_public_id"]),
                    version=publication["version"],
                    content_hash=publication["content_hash"],
                    payload=publication["payload"],
                )

                for entry in payload["activities"]:
                    _insert_trusted(
                        InstalledActivityVersion,
                        package=package,
                        activity_public_id=uuid.UUID(entry["activity_public_id"]),
                        version=entry["version"],
                        type_key=entry["type_key"],
                        config=entry["config"],
                    )

                question_rows = {}
                for entry in payload["questions"]:
                    question_public_id = uuid.UUID(entry["question_public_id"])
                    row = _insert_trusted(
                        InstalledQuestionVersion,
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
                    _insert_trusted(
                        InstalledQuestionGrading,
                        question=question_row,
                        answer_key=entry["answer_key"],
                        scoring=entry["scoring"],
                    )
        except IntegrityError:
            # Only ever a legitimate identity race, never suppressed
            # blindly: if no row with this exact identity exists after
            # the failure, this was some OTHER integrity violation and
            # must propagate.
            racing_existing = _find_installed_package(
                package_type, source_public_id, package_version
            )
            if racing_existing is None:
                raise
            if racing_existing.payload_sha256 == payload_sha256:
                return racing_existing
            raise PackageIdentityConflict(
                f"{package_type} {source_public_id} v{package_version} is already "
                "installed with a different payload hash"
            ) from None

    return package


def _require_complete_inventory(package):
    """Fail-closed defense-in-depth (Task 12 corrective): reject
    activation of an inventory row that could not have come from a
    genuine trusted import. This is NOT a re-run of Task 11's
    cryptographic/dependency-closure verification -- nothing here touches
    signatures or the activity registry, and it never will, because public
    ORM creation of InstalledPackage/artifact rows is already blocked
    entirely (linuxlab/sync/models.py). It only catches gross structural
    incompleteness in a row that reached the database through some other
    means (a corrupted/fabricated row introduced via a low-level
    bypass), as a second, independent line of defense.
    """
    if not InstalledPublication.objects.filter(package=package).exists():
        raise PackageActivationError(
            f"InstalledPackage {package.pk} has no staged publication artifact; "
            "refusing to activate an incomplete package"
        )

    question_count = InstalledQuestionVersion.objects.filter(package=package).count()
    grading_count = InstalledQuestionGrading.objects.filter(question__package=package).count()
    if question_count != grading_count:
        raise PackageActivationError(
            f"InstalledPackage {package.pk} has {question_count} staged questions but "
            f"{grading_count} grading rows; refusing to activate an incomplete package"
        )


def activate_package(package):
    """Atomically activate `package` (an InstalledPackage in state
    `staged`) within its own (package_type, source_public_id) scope.

    The currently active package in that scope, if any, becomes archived;
    `package` becomes active; both transitions happen in one transaction
    under a row lock covering every InstalledPackage row in that scope, so
    no concurrent activation of the same source can ever produce two
    simultaneously active packages. Activating a package that is already
    active is an idempotent no-op. An archived package can never be
    activated directly here -- only rollback_package() may reactivate one,
    which closes the previous_package cycle footgun a direct
    archived->active transition through this function would otherwise
    allow. Semantic artifact rows are never touched -- only lifecycle
    metadata changes.
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

        if candidate.state != STAGED:
            raise PackageActivationError(
                f"cannot activate a package in state {candidate.state!r}; an "
                "archived package can only be reactivated through rollback_package()"
            )

        _require_complete_inventory(candidate)

        current_active = next(
            (row for row in scope_rows if row.state == ACTIVE and row.pk != candidate.pk),
            None,
        )

        if current_active is not None:
            _write_installed_package_lifecycle(current_active.pk, state=ARCHIVED)

        _write_installed_package_lifecycle(
            candidate.pk,
            state=ACTIVE,
            activated_at=timezone.now(),
            previous_package=current_active.pk if current_active else None,
        )

        activated = InstalledPackage.objects.get(pk=candidate.pk)

    return activated


@contextmanager
def lock_active_installed_activity_for_execution(
    *,
    package_pk,
    package_type,
    source_public_id,
    package_version,
    payload_sha256,
    activity_public_id,
    version,
):
    """T15-D27 corrective (Important finding): the sync-owned lifecycle-
    lock boundary for starting a NEW installed Attempt.

    Locks the exact same (package_type, source_public_id) row scope, in
    the exact same pk order, that activate_package()/rollback_package()
    lock -- so a concurrent activation/rollback of the same source
    serializes on that shared lock instead of racing a caller that is
    about to insert an Attempt against "the currently active package".

    MUST be called inside the caller's own transaction.atomic(); the row
    lock is held for as long as that transaction remains open, which is
    what lets the caller safely create an Attempt against the yielded
    InstalledActivityVersion (while still inside this `with` block, or at
    least still inside the same outer transaction) without a concurrent
    activation archiving its package in between the check and the insert.

    Every identity field the caller claims is re-verified against the
    freshly locked row, not merely package_pk: the caller's
    RuntimePackageContext may have been opened (and its identity read)
    before this lock was ever acquired, so nothing about it is trusted
    without a fresh check under lock. Raises PackageActivationError if
    the package no longer exists, its identity no longer matches every
    claimed field, or it is not currently ACTIVE; raises
    InstalledActivityNotFoundError if no InstalledActivityVersion for
    (activity_public_id, version) exists in that (now-locked) package.
    """
    scope_rows = list(
        InstalledPackage.objects.select_for_update()
        .filter(package_type=package_type, source_public_id=source_public_id)
        .order_by("pk")
    )
    package = next((row for row in scope_rows if row.pk == package_pk), None)
    if package is None:
        raise PackageActivationError(f"InstalledPackage {package_pk} no longer exists")

    if (
        package.package_type != package_type
        or package.source_public_id != source_public_id
        or package.package_version != package_version
        or package.payload_sha256 != payload_sha256
    ):
        raise PackageActivationError(
            f"InstalledPackage {package_pk} identity does not match the caller's claim"
        )

    if package.state != ACTIVE:
        raise PackageActivationError(f"InstalledPackage {package_pk} is not active")

    installed_activity = InstalledActivityVersion.objects.filter(
        package=package, activity_public_id=activity_public_id, version=version
    ).first()
    if installed_activity is None:
        raise InstalledActivityNotFoundError(
            f"no activity {activity_public_id} v{version} in InstalledPackage {package_pk}"
        )

    yield installed_activity


def rollback_package(package_type, source_public_id):
    """Roll back the currently active package for
    (package_type, source_public_id) to its recorded previous_package.

    Never rebuilds, re-verifies, redownloads, or contacts Central: the
    previous package's staged artifacts are reused exactly as they were
    originally imported. Fails closed if there is no active package, no
    recorded previous package, or the recorded previous package does not
    belong to this exact source. The reactivated package's own
    previous_package link is left untouched -- rollback never manufactures
    a reciprocal link, so a second rollback from it follows its own
    genuine history, if any.
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

        _write_installed_package_lifecycle(current_active.pk, state=ARCHIVED)
        _write_installed_package_lifecycle(previous.pk, state=ACTIVE, activated_at=timezone.now())

        rolled_back_to = InstalledPackage.objects.get(pk=previous.pk)

    return rolled_back_to


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
