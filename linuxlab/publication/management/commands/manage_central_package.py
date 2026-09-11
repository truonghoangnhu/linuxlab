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
