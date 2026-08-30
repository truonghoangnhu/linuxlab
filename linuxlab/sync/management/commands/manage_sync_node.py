"""Task 14 controlled Node registration administration (T14-D23): issue,
rotate, and revoke only -- there is no self-service Node enrollment.

Usage:
    manage.py manage_sync_node issue --name "Lab A"
    manage.py manage_sync_node rotate <node_uuid> --grace-hours 24
    manage.py manage_sync_node revoke <node_uuid>
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from linuxlab.sync.authentication import (
    issue_node_registration,
    revoke_node_registration,
    rotate_node_credential,
)
from linuxlab.sync.models import NodeRegistration


class Command(BaseCommand):
    help = "Issue, rotate, or revoke a Task 14 Node sync credential."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)

        issue_parser = subparsers.add_parser("issue", help="Provision a new Node registration.")
        issue_parser.add_argument("--name", required=True, help="Human-readable Node label.")

        rotate_parser = subparsers.add_parser("rotate", help="Rotate an existing Node's secret.")
        rotate_parser.add_argument("node_id")
        rotate_parser.add_argument("--grace-hours", type=int, default=24)

        revoke_parser = subparsers.add_parser("revoke", help="Immediately disable a Node.")
        revoke_parser.add_argument("node_id")

    def _print_secret_once(self, credential):
        self.stdout.write(f"node_id: {credential.node_id}")
        self.stdout.write(f"secret: {credential.secret}")
        self.stdout.write(
            self.style.WARNING("Store this secret now -- it will not be shown again.")
        )

    def handle(self, *args, **options):
        action = options["action"]

        if action == "issue":
            credential = issue_node_registration(options["name"])
            self._print_secret_once(credential)
            return

        if action == "rotate":
            try:
                credential = rotate_node_credential(
                    options["node_id"], timedelta(hours=options["grace_hours"])
                )
            except (NodeRegistration.DoesNotExist, ValueError) as exc:
                raise CommandError(f"unknown node_id: {options['node_id']}") from exc
            self._print_secret_once(credential)
            return

        if action == "revoke":
            try:
                revoke_node_registration(options["node_id"])
            except (NodeRegistration.DoesNotExist, ValueError) as exc:
                raise CommandError(f"unknown node_id: {options['node_id']}") from exc
            self.stdout.write(self.style.SUCCESS(f"revoked {options['node_id']}"))
            return
