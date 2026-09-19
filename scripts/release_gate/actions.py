"""Task 15 Task 7: profile-scoped application action bridge (T15-D05,
T15-D17).

Every function here calls ONLY existing public/domain service functions --
create_question()/create_activity_definition() (Task 15 authoring root
public boundary corrective) for Question/ActivityDefinition root
authoring, create_question_version()/publish_question_version(),
create_activity_version()/publish_activity_version(),
publish_learning_path(), build_package(), import_package(),
activate_package(), issue_node_registration(). This module reproduces no
business logic of its own; it only sequences those existing calls with
explicit inputs/outputs so release-gate orchestration (a later task) can
drive Central/Node actions without embedding LinuxLab domain logic in
scripts/. There is no direct ORM creation of Question or
ActivityDefinition roots, no InstalledPackage.objects.create(), no
direct trusted Outbox/Inbox fabrication, and no answer-key material in
any return value here.

Every function assumes Django has ALREADY been configured (django.setup())
for the correct profile+database by its caller -- this module has no
opinion about which process/database it runs against. A Central-only
function called against a Node database (or vice versa) fails through the
real profile-capability guards application code already enforces (e.g.
import_package() itself checks settings.LINUXLAB_CAN_IMPORT_PACKAGES),
never through anything special in this module.

Provider is locked: assessment/single_choice, via the same production
default registries (build_default_activity_registry()/
build_default_question_registry()) every other runtime/service code path
uses -- never a release-gate-only or test-only registry.
"""

import uuid
from dataclasses import dataclass

from django.conf import settings

from linuxlab.activities.default_registry import build_default_activity_registry
from linuxlab.activities.services import (
    create_activity_definition,
    create_activity_version,
    publish_activity_version,
)
from linuxlab.assessment.default_registry import build_default_question_registry
from linuxlab.assessment.services import (
    create_question,
    create_question_version,
    publish_question_version,
)
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.package import build_package as _build_package
from linuxlab.publication.snapshots import publish_learning_path
from linuxlab.sync.authentication import (
    NodeRegistrationError,
    NodeRegistrationInvariantError,
    get_active_node_registration,
    issue_node_registration,
)
from linuxlab.sync.authentication import revoke_node_registration as _revoke_node_registration
from linuxlab.sync.models import InboxMessage, InstalledPackage, OutboxMessage
from linuxlab.sync.services import activate_package as _activate_package
from linuxlab.sync.services import get_active_package
from linuxlab.sync.services import import_package as _import_package


@dataclass(frozen=True)
class AuthoredSingleChoiceContent:
    """Stable public identities for the real, published single-choice
    assessment content author_and_publish_single_choice_activity() just
    created -- never local database primary keys -- plus the actual
    PublicationSnapshot instance build_and_sign_package() needs next."""

    source_public_id: object
    package_version: int
    activity_public_id: object
    activity_version: int
    question_public_id: object
    question_version: int
    snapshot: object


def _resolve_wagtail_root_page():
    """Idempotent Wagtail root-page resolution: a genuinely fresh
    migrated Central database already has the root page wagtailcore's own
    migration seeds, so this is normally just a lookup -- the add_root()
    fallback exists only so this bridge is also correct against a
    database where that data is absent for any reason, without depending
    on any test-only fixture module."""
    from wagtail.coreutils import get_supported_content_language_variant
    from wagtail.models import Locale, Page

    language_code = get_supported_content_language_variant(settings.LANGUAGE_CODE)
    Locale.objects.get_or_create(language_code=language_code)

    root = Page.get_first_root_node()
    if root is None:
        root = Page.add_root(instance=Page(title="Root", slug="root"))
    return root


def author_and_publish_single_choice_activity(
    *,
    prompt,
    options,
    correct_option_id,
    max_score=1,
    question_name="Release gate question",
    activity_name="Release gate activity",
    path_title="Release Gate Path",
    module_title="Release Gate Module",
    lesson_title="Release Gate Lesson",
):
    """Central-only. Authors ONE real assessment/single_choice Question
    and ActivityDefinition, plus a LearningPath/Module/Lesson referencing
    it, and publishes all three (QuestionVersion, ActivityVersion,
    PublicationSnapshot) through their existing public/domain services --
    exactly what a human author would do through the same APIs.
    """
    question_registry = build_default_question_registry()
    activity_registry = build_default_activity_registry()

    question = create_question(name=question_name)
    question_version = create_question_version(
        question,
        type_key="single_choice",
        content={"prompt": prompt, "options": options},
        answer_key={"correct_option_id": correct_option_id},
        scoring={"max_score": max_score},
    )
    published_question = publish_question_version(question_version, question_registry)

    definition = create_activity_definition(name=activity_name, type_key="assessment")
    activity_version = create_activity_version(
        definition,
        config={
            "questions": [
                {
                    "question_public_id": str(published_question.question.public_id),
                    "question_version": published_question.version,
                }
            ]
        },
    )
    published_activity = publish_activity_version(activity_version, activity_registry)

    root = _resolve_wagtail_root_page()
    path = LearningPathPage(title=path_title)
    root.add_child(instance=path)
    module = ModulePage(title=module_title)
    path.add_child(instance=module)
    lesson = LessonPage(title=lesson_title)
    lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(definition.public_id),
                "activity_version": published_activity.version,
            },
        )
    ]
    module.add_child(instance=lesson)

    snapshot = publish_learning_path(path, activity_registry)

    return AuthoredSingleChoiceContent(
        source_public_id=snapshot.source_public_id,
        package_version=snapshot.version,
        activity_public_id=definition.public_id,
        activity_version=published_activity.version,
        question_public_id=published_question.question.public_id,
        question_version=published_question.version,
        snapshot=snapshot,
    )


def build_and_sign_package(snapshot, private_key):
    """Central-only. Builds and Ed25519-signs a real `.llpkg` archive
    from an already-published PublicationSnapshot, using the same
    production default activity registry ordinary application code uses.
    Returns raw archive bytes -- never touches signing key material
    beyond passing it straight through to the existing build_package()
    service."""
    activity_registry = build_default_activity_registry()
    return _build_package(snapshot, activity_registry, private_key)


def republish_learning_path(source_public_id):
    """Central-only. Publishes a NEW PublicationSnapshot version from the
    live LearningPathPage identified by `source_public_id`, using the
    same production default activity registry -- for scenarios (e.g.
    package-pinning proofs) that need a second distinct package_version
    of the same source."""
    activity_registry = build_default_activity_registry()
    live_path = LearningPathPage.objects.get(public_id=source_public_id)
    return publish_learning_path(live_path, activity_registry)


def issue_node_credential(display_name):
    """Central-only. Issues a real Node registration + bearer credential
    through the existing Task 14 public/domain service. The plaintext
    secret is returned exactly once, as issue_node_registration() already
    guarantees; this bridge never persists it."""
    return issue_node_registration(display_name)


def find_publication_snapshot(source_public_id, package_version):
    """Central-only, READ-ONLY. Returns the exact PublicationSnapshot for
    (kind="learning_path", source_public_id, package_version), or None --
    used to prove published content/package metadata survives a backup/
    restore cycle."""
    from linuxlab.publication.models import PublicationSnapshot

    return PublicationSnapshot.objects.filter(
        kind="learning_path", source_public_id=source_public_id, version=package_version
    ).first()


def _node_uuid(node_id):
    """Canonical registration selector at this adapter boundary
    (Corrective B Task 13): the Task-5 services accept ONLY a
    uuid.UUID, so a textual selector from cli.py argv is converted and
    validated HERE, before any service call. A malformed value raises
    ValueError (uuid.UUID's own, which never echoes the value) or
    TypeError; nothing is ever inferred from message text."""
    if isinstance(node_id, uuid.UUID):
        return node_id
    if isinstance(node_id, str):
        return uuid.UUID(node_id)
    raise TypeError("node_id must be a UUID or its canonical text form")


def find_node_registration(node_id):
    """Central-only, READ-ONLY. Returns the exactly-one ACTIVE
    NodeRegistration for the logical `node_id` through the Task-5
    authority (get_active_node_registration()), or None when that
    logical node has zero ACTIVE authority (revoked, superseded-only,
    unknown). The multiple-ACTIVE invariant failure propagates unchanged.
    Never a first-row shortcut, never a direct registration query, never
    newest/PK/timestamp selection -- separate E2E evidence, not
    diagnostic remediation."""
    selector = _node_uuid(node_id)
    try:
        return get_active_node_registration(selector)
    except NodeRegistrationInvariantError:
        raise
    except NodeRegistrationError:
        return None


def find_inbox_message(event_id):
    """Central-only, READ-ONLY. Returns the exact durable Inbox record
    for `event_id`, or None. Assertion support only -- this bridge never
    creates, mutates, or fabricates an InboxMessage row; every row it
    reads was written exclusively by linuxlab.sync.ingestion in response
    to a real HTTP POST to /api/v1/sync/events/."""
    return InboxMessage.objects.filter(event_id=event_id).first()


def count_inbox_messages(event_id):
    """Central-only, READ-ONLY. Exactly how many durable Inbox rows exist
    for `event_id` -- used to prove idempotent duplicate delivery leaves
    precisely one row, never zero or more than one."""
    return InboxMessage.objects.filter(event_id=event_id).count()


def revoke_node_credential(node_id):
    """Central-only. Immediately revokes a Node's ACTIVE registration
    through the existing Task-5 public/domain service
    (revoke_node_registration(node_id: UUID)) -- used by the N2
    invalid/revoked-credential negative scenario. The selector is
    converted/validated to the canonical UUID here, before the service
    call (Corrective B Task 13); the service is never weakened to accept
    text."""
    _revoke_node_registration(_node_uuid(node_id))


def verify_import_and_activate_package(package_bytes, trusted_public_key):
    """Node-only. Verifies, imports (stages), and activates a real signed
    `.llpkg` through the exact Task 11/12 public trust boundary --
    verify_package()'s cryptographic/structural/dependency checks run
    inside import_package() itself; there is no
    InstalledPackage.objects.create() or any other bypass here. Raises
    PackageImportError for a tampered/invalid package -- this bridge
    performs no pre-filtering of that outcome."""
    activity_registry = build_default_activity_registry()
    installed = _import_package(package_bytes, activity_registry, trusted_public_key)
    return _activate_package(installed)


def find_outbox_message(event_id):
    """Node-only, READ-ONLY. Returns the exact local Outbox record for
    `event_id`, or None. Assertion support only."""
    return OutboxMessage.objects.filter(event_id=event_id).first()


def find_outbox_message_by_attempt(attempt_public_id):
    """Node-only, READ-ONLY. Returns the local Outbox record whose
    aggregate_public_id is `attempt_public_id` (the Task 14 v1 wire
    contract: aggregate_type="attempt", aggregate_public_id=attempt.
    public_id -- unchanged, unversioned, T15-D27 final corrective S12),
    or None. Lets a caller that only knows the Attempt's public_id (e.g.
    from an HTTP submit response) find its Outbox row without needing to
    separately track the event_id."""
    return OutboxMessage.objects.filter(
        aggregate_type="attempt", aggregate_public_id=attempt_public_id
    ).first()


def count_installed_packages(package_type, source_public_id):
    """Node-only, READ-ONLY. Exactly how many InstalledPackage rows exist
    for (package_type, source_public_id) across every lifecycle state
    (staged/active/archived) -- used by the N1 invalid-signature scenario
    to prove a failed/rejected import leaves NO partial trusted inventory
    row at all, not merely that none became active."""
    return InstalledPackage.objects.filter(
        package_type=package_type, source_public_id=source_public_id
    ).count()


def find_active_package(package_type, source_public_id):
    """Node-only, READ-ONLY. Returns the exactly-one ACTIVE InstalledPackage
    for (package_type, source_public_id), or None -- the same public
    lookup Node runtime itself uses (linuxlab.sync.services.
    get_active_package()), used here only for scenario assertions."""
    return get_active_package(package_type, source_public_id)


def find_attempt(attempt_public_id):
    """Node-only, READ-ONLY. Returns the exact local Attempt for
    `attempt_public_id`, or None -- for provenance assertions (exact
    pinned InstalledActivityVersion/package identity), never used to
    create or mutate anything."""
    from linuxlab.learning.services import get_attempt_by_public_id

    return get_attempt_by_public_id(attempt_public_id)


def find_progress(learner_id, activity_definition_public_id):
    """Node-only, READ-ONLY. Returns the exact local Progress row for
    (learner_id, activity_definition_public_id), or None, plus how many
    such rows exist -- used to prove exactly one logical Progress record
    exists per learner/activity, never zero or more than one."""
    from linuxlab.learning.models import Progress

    queryset = Progress.objects.filter(
        learner_id=learner_id, activity_definition_public_id=activity_definition_public_id
    )
    return queryset.count(), queryset.first()


def resend_outbox_message(event_id, central_url, node_id, secret, *, allow_insecure_http=True):
    """Node-only. Rebuilds the EXACT wire envelope originally captured
    for an existing local OutboxMessage row (envelope_for() -- never a
    reserialization of current domain state) and resends it to Central
    through a real HttpSyncTransport, exactly as a genuine Node retry
    after a lost ACK would. Used to prove Central's idempotent duplicate
    handling (T15-D09 N3) without ever touching trusted Outbox/Inbox
    state directly -- this never mutates the local OutboxMessage row's
    delivery metadata itself (that remains flush_outbox()'s job); it only
    performs one additional real HTTP delivery attempt of an event that
    may already have DELIVERED, and returns Central's ack.
    """
    from linuxlab.sync.contracts import BatchAck
    from linuxlab.sync.outbox import envelope_for
    from linuxlab.sync.transports import HttpSyncTransport

    row = OutboxMessage.objects.get(event_id=event_id)
    envelope = envelope_for(row)
    transport = HttpSyncTransport(
        central_url, node_id, secret, allow_insecure_http=allow_insecure_http
    )
    batch_ack: BatchAck = transport.send_batch((envelope,))
    return batch_ack
