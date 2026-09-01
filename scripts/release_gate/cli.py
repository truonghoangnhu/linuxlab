"""Task 15 Task 7: standalone subprocess entry point for
scripts.release_gate.actions.

Each invocation does `django.setup()` ONCE using DJANGO_SETTINGS_MODULE +
POSTGRES_DB from its own process environment (set by the caller -- e.g.
Task 8's dual-deployment E2E -- exactly like
scripts.release_gate.database.run_migrations() already does for
`manage.py migrate`), dispatches to exactly one action in actions.py,
and prints a single JSON object to stdout. This script contains no
LinuxLab business logic itself -- every subcommand is a thin
argument-parsing/JSON-formatting wrapper around one existing
scripts.release_gate.actions function, which in turn only orchestrates
existing public/domain application services (T15-D05, T15-D17).

Exit code 0 with a JSON object on stdout on success; non-zero with a
JSON {"error": "..."} object on stdout on any failure -- never a Python
traceback on stdout, so a caller can always `json.loads(stdout)`
regardless of outcome.
"""

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _setup_django():
    import django

    django.setup()


def _load_private_key(path):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    return load_pem_private_key(Path(path).read_bytes(), password=None)


def _load_public_key(path):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    return load_pem_public_key(Path(path).read_bytes())


def _cmd_author_and_publish(args):
    from scripts.release_gate import actions

    authored = actions.author_and_publish_single_choice_activity(
        prompt=args.prompt,
        options=json.loads(args.options_json),
        correct_option_id=args.correct_option_id,
        max_score=args.max_score,
    )
    return {
        "source_public_id": str(authored.source_public_id),
        "package_version": authored.package_version,
        "activity_public_id": str(authored.activity_public_id),
        "activity_version": authored.activity_version,
        "question_public_id": str(authored.question_public_id),
        "question_version": authored.question_version,
    }


def _cmd_republish(args):
    from scripts.release_gate import actions

    snapshot = actions.republish_learning_path(args.source_public_id)
    return {"source_public_id": str(snapshot.source_public_id), "package_version": snapshot.version}


def _cmd_build_and_sign_package(args):
    from linuxlab.publication.models import PublicationSnapshot
    from scripts.release_gate import actions

    private_key = _load_private_key(args.private_key_file)
    snapshot = PublicationSnapshot.objects.get(
        kind="learning_path", source_public_id=args.source_public_id, version=args.package_version
    )
    package_bytes = actions.build_and_sign_package(snapshot, private_key)
    Path(args.output_file).write_bytes(package_bytes)
    return {"output_file": args.output_file, "size": len(package_bytes)}


def _cmd_issue_node_credential(args):
    from scripts.release_gate import actions

    credential = actions.issue_node_credential(args.display_name)
    return {"node_id": str(credential.node_id), "secret": credential.secret}


def _cmd_find_inbox_message(args):
    from scripts.release_gate import actions

    found = actions.find_inbox_message(args.event_id)
    if found is None:
        return {"found": False}
    return {
        "found": True,
        "event_id": str(found.event_id),
        "event_type": found.event_type,
        "aggregate_type": found.aggregate_type,
        "aggregate_public_id": str(found.aggregate_public_id),
    }


def _cmd_count_inbox_messages(args):
    from scripts.release_gate import actions

    return {"count": actions.count_inbox_messages(args.event_id)}


def _cmd_import_and_activate_package(args):
    from scripts.release_gate import actions

    public_key = _load_public_key(args.public_key_file)
    package_bytes = Path(args.package_file).read_bytes()
    activated = actions.verify_import_and_activate_package(package_bytes, public_key)
    return {
        "package_type": activated.package_type,
        "source_public_id": str(activated.source_public_id),
        "package_version": activated.package_version,
        "state": activated.state,
    }


def _cmd_find_outbox_message(args):
    from scripts.release_gate import actions

    found = actions.find_outbox_message(args.event_id)
    if found is None:
        return {"found": False}
    return {
        "found": True,
        "event_id": str(found.event_id),
        "state": found.state,
        "attempts": found.attempts,
    }


def _cmd_find_outbox_message_by_attempt(args):
    from scripts.release_gate import actions

    found = actions.find_outbox_message_by_attempt(args.attempt_public_id)
    if found is None:
        return {"found": False}
    return {
        "found": True,
        "event_id": str(found.event_id),
        "event_type": found.event_type,
        "aggregate_type": found.aggregate_type,
        "aggregate_public_id": str(found.aggregate_public_id),
        "state": found.state,
        "attempts": found.attempts,
    }


def _cmd_find_attempt(args):
    from scripts.release_gate import actions

    attempt = actions.find_attempt(args.attempt_public_id)
    if attempt is None:
        return {"found": False}
    installed = attempt.installed_activity_version
    result = getattr(attempt, "result", None)
    return {
        "found": True,
        "attempt_public_id": str(attempt.public_id),
        "learner_id": attempt.learner_id,
        "learner_public_id": str(attempt.learner.public_id),
        "activity_version_id": attempt.activity_version_id,
        "installed_activity_public_id": str(installed.activity_public_id) if installed else None,
        "installed_activity_version": installed.version if installed else None,
        "installed_package_source_public_id": (
            str(installed.package.source_public_id) if installed else None
        ),
        "installed_package_version": installed.package.package_version if installed else None,
        "submitted_at": attempt.submitted_at.isoformat() if attempt.submitted_at else None,
        "result_score": result.score if result else None,
        "result_max_score": result.max_score if result else None,
    }


def _cmd_find_progress(args):
    from scripts.release_gate import actions

    count, progress = actions.find_progress(args.learner_id, args.activity_definition_public_id)
    if progress is None:
        return {"found": False, "count": count}
    return {
        "found": True,
        "count": count,
        "completed": progress.completed,
        "best_score": progress.best_score,
    }


def _cmd_resend_outbox_message(args):
    from scripts.release_gate import actions

    ack = actions.resend_outbox_message(
        args.event_id, args.central_url, args.node_id, args.secret
    )
    return {
        "results": [
            {"event_id": str(result.event_id), "status": result.status, "code": result.code}
            for result in ack.results
        ]
    }


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    author = subparsers.add_parser("author-and-publish")
    author.add_argument("--prompt", required=True)
    author.add_argument("--options-json", required=True)
    author.add_argument("--correct-option-id", required=True)
    author.add_argument("--max-score", type=int, default=1)
    author.set_defaults(func=_cmd_author_and_publish)

    republish = subparsers.add_parser("republish")
    republish.add_argument("--source-public-id", required=True)
    republish.set_defaults(func=_cmd_republish)

    build_sign = subparsers.add_parser("build-and-sign-package")
    build_sign.add_argument("--source-public-id", required=True)
    build_sign.add_argument("--package-version", type=int, required=True)
    build_sign.add_argument("--private-key-file", required=True)
    build_sign.add_argument("--output-file", required=True)
    build_sign.set_defaults(func=_cmd_build_and_sign_package)

    issue_credential = subparsers.add_parser("issue-node-credential")
    issue_credential.add_argument("--display-name", required=True)
    issue_credential.set_defaults(func=_cmd_issue_node_credential)

    find_inbox = subparsers.add_parser("find-inbox-message")
    find_inbox.add_argument("--event-id", required=True)
    find_inbox.set_defaults(func=_cmd_find_inbox_message)

    count_inbox = subparsers.add_parser("count-inbox-messages")
    count_inbox.add_argument("--event-id", required=True)
    count_inbox.set_defaults(func=_cmd_count_inbox_messages)

    import_activate = subparsers.add_parser("import-and-activate-package")
    import_activate.add_argument("--package-file", required=True)
    import_activate.add_argument("--public-key-file", required=True)
    import_activate.set_defaults(func=_cmd_import_and_activate_package)

    find_outbox = subparsers.add_parser("find-outbox-message")
    find_outbox.add_argument("--event-id", required=True)
    find_outbox.set_defaults(func=_cmd_find_outbox_message)

    find_outbox_by_attempt = subparsers.add_parser("find-outbox-message-by-attempt")
    find_outbox_by_attempt.add_argument("--attempt-public-id", required=True)
    find_outbox_by_attempt.set_defaults(func=_cmd_find_outbox_message_by_attempt)

    find_attempt = subparsers.add_parser("find-attempt")
    find_attempt.add_argument("--attempt-public-id", required=True)
    find_attempt.set_defaults(func=_cmd_find_attempt)

    find_progress = subparsers.add_parser("find-progress")
    find_progress.add_argument("--learner-id", type=int, required=True)
    find_progress.add_argument("--activity-definition-public-id", required=True)
    find_progress.set_defaults(func=_cmd_find_progress)

    resend_outbox = subparsers.add_parser("resend-outbox-message")
    resend_outbox.add_argument("--event-id", required=True)
    resend_outbox.add_argument("--central-url", required=True)
    resend_outbox.add_argument("--node-id", required=True)
    resend_outbox.add_argument("--secret", required=True)
    resend_outbox.set_defaults(func=_cmd_resend_outbox_message)

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        _setup_django()
        result = args.func(args)
    except Exception as exc:  # noqa: BLE001 -- always report as sanitized JSON, never a raw traceback on stdout
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
