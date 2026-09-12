import os
import uuid
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from linuxlab.activities.default_registry import build_default_activity_registry
from linuxlab.content.models import LearningPathPage
from linuxlab.publication.models import PublicationSnapshot
from linuxlab.publication.package import PackageBuildError, build_package
from linuxlab.publication.signing import (
    PackageSigningConfigurationError,
    load_package_signing_private_key,
)
from linuxlab.publication.snapshots import publish_learning_path


def _require_central_capability():
    if not getattr(settings, "LINUXLAB_CAN_AUTHOR", False):
        raise CommandError(
            "package publication is not permitted on this deployment profile"
        )


def _parse_uuid(raw, *, what):
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise CommandError(f"{what} is not a valid UUID: {raw!r}") from exc


def _parse_version(raw):
    try:
        version = int(raw)
    except (ValueError, TypeError) as exc:
        raise CommandError(f"--version is not a valid version number: {raw!r}") from exc
    if version < 1:
        raise CommandError(f"--version is not a valid version number: {raw!r}")
    return version


def _write_new_file_atomically(destination, data: bytes) -> None:
    """Publish `data` at `destination` without ever overwriting an
    existing file there, and without ever leaving a partial final file
    or a leaked temp file on any failure path this invocation is
    actually responsible for.

    Strategy: write the full bytes to a temp file in the SAME directory
    as `destination` (so the later link is a same-filesystem, atomic
    operation), fsync+close it, then `os.link()` the temp file onto the
    final path. `os.link()` raises FileExistsError atomically if the
    destination already exists (unlike `os.replace()`, which silently
    overwrites) -- this closes the TOCTOU race an `if not path.exists()`
    check followed by a separate write/rename would leave open.

    Ownership: this invocation may only unlink temp_path once it has
    itself successfully created that exact entry via exclusive (O_EXCL)
    creation. If that creation fails -- including because a same-named
    entry already exists and belongs to someone else -- this invocation
    never owned it and must not touch it. Cleanup afterwards is
    best-effort and must never replace/mask an already-established
    primary failure; a cleanup-only failure (nothing else went wrong) is
    still surfaced explicitly rather than silently ignored.
    """
    parent = destination.parent
    temp_path = parent / f".{destination.name}.tmp-{os.getpid()}-{id(data)}"

    try:
        fd = os.open(str(temp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError as exc:
        # temp_path was never created by this invocation: it is not ours
        # to remove, whether it failed because it already existed or for
        # any other reason (e.g. a non-directory parent).
        raise CommandError(
            f"failed to write package output at {destination}: {exc.strerror}"
        ) from exc

    primary_error = None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        os.link(str(temp_path), str(destination))
    except FileExistsError as exc:
        primary_error = CommandError(f"output already exists: {destination}")
        primary_error.__cause__ = exc
    except OSError as exc:
        primary_error = CommandError(
            f"failed to write package output at {destination}: {exc.strerror}"
        )
        primary_error.__cause__ = exc

    # This invocation successfully created temp_path above, so it -- and
    # only it -- is responsible for removing it.
    try:
        temp_path.unlink()
    except OSError as cleanup_exc:
        if primary_error is None:
            # Nothing else failed: the output was written and linked
            # successfully, so a cleanup-only failure here is a distinct,
            # expected filesystem problem worth surfacing on its own
            # rather than silently treating overall cleanup as having
            # succeeded.
            raise CommandError(
                f"package output written at {destination} but failed to "
                f"remove temporary file {temp_path}: {cleanup_exc.strerror}"
            ) from cleanup_exc
        # A primary failure already occurred; best-effort cleanup of our
        # own owned temp file must not replace/mask it.

    if primary_error is not None:
        raise primary_error


class Command(BaseCommand):
    help = "Central package operator surface: publish, build."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)

        publish_parser = subparsers.add_parser("publish")
        publish_parser.add_argument("--learning-path", required=True)

        # --version and --learning-path are deliberately plain strings
        # (no argparse type=int/type=uuid.UUID): argparse-level type
        # coercion runs before handle() is ever called, which would let a
        # malformed value reject the command before
        # _require_central_capability() has a chance to run first. Both
        # are parsed manually, inside _handle_build(), strictly after the
        # capability guard.
        build_parser = subparsers.add_parser("build")
        build_parser.add_argument("--learning-path", required=True)
        build_parser.add_argument("--version", required=True)
        build_parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        _require_central_capability()

        if options["action"] == "publish":
            self._handle_publish(options)
            return

        if options["action"] == "build":
            self._handle_build(options)
            return

    def _handle_publish(self, options):
        learning_path_id = _parse_uuid(options["learning_path"], what="--learning-path")

        try:
            page = LearningPathPage.objects.get(public_id=learning_path_id)
        except LearningPathPage.DoesNotExist as exc:
            raise CommandError(f"LearningPath not found: {learning_path_id}") from exc

        registry = build_default_activity_registry()

        try:
            snapshot = publish_learning_path(page, registry)
        except ValueError as exc:
            raise CommandError(f"publication rejected: {exc}") from exc

        self.stdout.write("Operation: publish")
        self.stdout.write(f"Kind: {snapshot.kind}")
        self.stdout.write(f"LearningPath: {snapshot.source_public_id}")
        self.stdout.write(f"Version: {snapshot.version}")

    def _handle_build(self, options):
        source_public_id = _parse_uuid(options["learning_path"], what="--learning-path")
        version = _parse_version(options["version"])

        try:
            snapshot = PublicationSnapshot.objects.get(
                kind="learning_path", source_public_id=source_public_id, version=version
            )
        except PublicationSnapshot.DoesNotExist as exc:
            raise CommandError(
                f"no publication for LearningPath {source_public_id} at version {version}"
            ) from exc

        output_path = Path(options["output"])
        if not output_path.parent.is_dir():
            raise CommandError(f"output directory does not exist: {output_path.parent}")
        if output_path.exists():
            raise CommandError(f"output already exists: {output_path}")

        try:
            private_key = load_package_signing_private_key()
        except PackageSigningConfigurationError as exc:
            raise CommandError(f"signing configuration unavailable: {exc}") from exc

        registry = build_default_activity_registry()
        try:
            package_bytes = build_package(snapshot, registry, private_key)
        except PackageBuildError as exc:
            raise CommandError(f"package build rejected: {exc}") from exc

        _write_new_file_atomically(output_path, package_bytes)

        self.stdout.write("Operation: build")
        self.stdout.write(f"Kind: {snapshot.kind}")
        self.stdout.write(f"LearningPath: {snapshot.source_public_id}")
        self.stdout.write(f"Version: {snapshot.version}")
        self.stdout.write(f"PackageType: {snapshot.kind}")
        self.stdout.write(f"PackageVersion: {snapshot.version}")
        self.stdout.write(f"PackageUUID: {snapshot.source_public_id}")
        self.stdout.write(f"Output: {output_path}")
