from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from linuxlab.activities.default_registry import build_default_activity_registry
from linuxlab.publication.package import PackageVerificationError, verify_package
from linuxlab.sync.services import PackageImportError, import_package
from linuxlab.sync.trust import PackageTrustConfigurationError, load_package_trusted_public_key


def _require_node_capability():
    if not getattr(settings, "LINUXLAB_CAN_IMPORT_PACKAGES", False):
        raise CommandError(
            "package verify/import is not permitted on this deployment profile"
        )


def _load_trust_anchor():
    try:
        return load_package_trusted_public_key()
    except PackageTrustConfigurationError as exc:
        raise CommandError(f"trust anchor unavailable: {exc}") from exc


class Command(BaseCommand):
    help = "Node package operator surface: verify, import."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)

        verify_parser = subparsers.add_parser("verify")
        verify_parser.add_argument("--file", required=True)

        import_parser = subparsers.add_parser("import")
        import_parser.add_argument("--file", required=True)

    def handle(self, *args, **options):
        _require_node_capability()

        if options["action"] == "verify":
            self._handle_verify(options)
            return

        if options["action"] == "import":
            self._handle_import(options)
            return

    def _handle_verify(self, options):
        trusted_public_key = _load_trust_anchor()

        try:
            package_bytes = Path(options["file"]).read_bytes()
        except OSError as exc:
            raise CommandError(
                f"failed to read package file at {options['file']}: {exc.strerror}"
            ) from exc

        registry = build_default_activity_registry()

        try:
            verified = verify_package(package_bytes, registry, trusted_public_key)
        except PackageVerificationError as exc:
            raise CommandError(f"package verification failed: {exc}") from exc

        self.stdout.write("Operation: verify")
        self.stdout.write(f"PackageType: {verified.manifest.package_type}")
        self.stdout.write(f"Source: {verified.manifest.package_uuid}")
        self.stdout.write(f"Version: {verified.manifest.package_version}")

    def _handle_import(self, options):
        trusted_public_key = _load_trust_anchor()

        try:
            package_bytes = Path(options["file"]).read_bytes()
        except OSError as exc:
            raise CommandError(
                f"failed to read package file at {options['file']}: {exc.strerror}"
            ) from exc

        registry = build_default_activity_registry()

        try:
            installed = import_package(package_bytes, registry, trusted_public_key)
        except PackageImportError as exc:
            raise CommandError(f"package import failed: {exc}") from exc

        self.stdout.write("Operation: import")
        self.stdout.write(f"PackageType: {installed.package_type}")
        self.stdout.write(f"Source: {installed.source_public_id}")
        self.stdout.write(f"Version: {installed.package_version}")
        self.stdout.write(f"State: {installed.state}")
