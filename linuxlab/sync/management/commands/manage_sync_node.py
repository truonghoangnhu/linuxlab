"""Controlled Node registration administration (T14-D23; Corrective B Task
5): issue, rotate, and revoke only -- there is no self-service Node
enrollment, and no grace window.

Usage:
    manage.py manage_sync_node issue --name "Lab A"
    manage.py manage_sync_node issue --name "Lab A" --node-id <uuid>
    manage.py manage_sync_node rotate <node_uuid>
    manage.py manage_sync_node revoke <node_uuid>
"""

import uuid

from django.core.management.base import BaseCommand, CommandError

from linuxlab.sync.authentication import (
    NodeRegistrationError,
    issue_node_registration,
    revoke_node_registration,
    rotate_node_credential,
)
from linuxlab.sync.capabilities import can_receive_sync_events


class Command(BaseCommand):
    help = "Issue, rotate, or revoke a Node sync credential."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)

        issue_parser = subparsers.add_parser("issue", help="Provision a new Node registration.")
        issue_parser.add_argument("--name", required=True, help="Human-readable Node label.")
        issue_parser.add_argument(
            "--node-id",
            type=uuid.UUID,
            default=None,
            help=(
                "Explicit logical node UUID to (re)provision; only valid when that "
                "node currently has zero ACTIVE registrations."
            ),
        )

        rotate_parser = subparsers.add_parser("rotate", help="Rotate an existing Node's secret.")
        rotate_parser.add_argument("node_id", type=uuid.UUID)

        revoke_parser = subparsers.add_parser("revoke", help="Immediately disable a Node.")
        revoke_parser.add_argument("node_id", type=uuid.UUID)

    def _print_secret_once(self, credential):
        self.stdout.write(f"node_id: {credential.node_id}")
        self.stdout.write(f"secret: {credential.secret}")
        self.stdout.write(
            self.style.WARNING("Store this secret now -- it will not be shown again.")
        )

    def handle(self, *args, **options):
        if not can_receive_sync_events():
            raise CommandError(
                "Node credential provisioning is not enabled on this deployment profile"
            )

        action = options["action"]

        if action == "issue":
            try:
                credential = issue_node_registration(
                    options["name"], node_id=options["node_id"]
                )
            except NodeRegistrationError as exc:
                raise CommandError(str(exc)) from exc
            self._print_secret_once(credential)
            return

        if action == "rotate":
            try:
                credential = rotate_node_credential(options["node_id"])
            except NodeRegistrationError as exc:
                raise CommandError(str(exc)) from exc
            self._print_secret_once(credential)
            return

        if action == "revoke":
            try:
                revoke_node_registration(options["node_id"])
            except NodeRegistrationError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(self.style.SUCCESS(f"revoked {options['node_id']}"))
            return
