import os
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from linuxlab.activities.default_registry import build_default_activity_registry
from linuxlab.content.models import LearningPathPage
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


def _write_new_file_atomically(destination, data: bytes) -> None:
    """Publish `data` at `destination` without ever overwriting an
    existing file there, and without ever leaving a partial final file
    or a leaked temp file on any failure path.

    Strategy: write the full bytes to a temp file in the SAME directory
    as `destination` (so the later link is a same-filesystem, atomic
    operation), fsync+close it, then `os.link()` the temp file onto the
    final path. `os.link()` raises FileExistsError atomically if the
    destination already exists (unlike `os.replace()`, which silently
    overwrites) -- this closes the TOCTOU race an `if not path.exists()`
    check followed by a separate write/rename would leave open. The temp
    file is always removed in `finally`, whether the link succeeded or
    not, so no leaked temp artifact remains on any exit path.

    Every expected filesystem OSError anywhere in this open/write/flush/
    fsync/link lifecycle -- not only the destination-collision case --
    is translated into a sanitized CommandError; only the destination
    already existing gets its own specific message.
    """
    parent = destination.parent
    temp_path = parent / f".{destination.name}.tmp-{os.getpid()}-{id(data)}"

    try:
        fd = os.open(str(temp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())

            os.link(str(temp_path), str(destination))
        except FileExistsError as exc:
            raise CommandError(f"output already exists: {destination}") from exc
    except OSError as exc:
        raise CommandError(
            f"failed to write package output at {destination}: {exc.strerror}"
        ) from exc
    finally:
        temp_path.unlink(missing_ok=True)


class Command(BaseCommand):
    help = "Central package operator surface: publish."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)

        publish_parser = subparsers.add_parser("publish")
        publish_parser.add_argument("--learning-path", required=True)

    def handle(self, *args, **options):
        _require_central_capability()

        if options["action"] == "publish":
            self._handle_publish(options)
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
