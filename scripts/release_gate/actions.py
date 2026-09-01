"""Task 15 Task 7: profile-scoped application action bridge (T15-D05,
T15-D17).

Every function here calls ONLY existing public/domain service functions
already used throughout linuxlab's own application code and test suite --
Question/ActivityDefinition authoring, create_question_version()/
publish_question_version(), create_activity_version()/
publish_activity_version(), publish_learning_path(), build_package(),
import_package(), activate_package(), issue_node_registration(). This
module reproduces no business logic of its own; it only sequences those
existing calls with explicit inputs/outputs so release-gate orchestration
(a later task) can drive Central/Node actions without embedding LinuxLab
domain logic in scripts/. There is no InstalledPackage.objects.create(),
no direct trusted Outbox/Inbox fabrication, and no answer-key material in
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

from dataclasses import dataclass

from django.conf import settings

from linuxlab.activities.default_registry import build_default_activity_registry
from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.assessment.default_registry import build_default_question_registry
from linuxlab.assessment.models import Question
from linuxlab.assessment.services import create_question_version, publish_question_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.package import build_package as _build_package
from linuxlab.publication.snapshots import publish_learning_path
from linuxlab.sync.authentication import issue_node_registration
from linuxlab.sync.models import InboxMessage, OutboxMessage
from linuxlab.sync.services import activate_package as _activate_package
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

    question = Question.objects.create(name=question_name)
    question_version = create_question_version(
        question,
        type_key="single_choice",
        content={"prompt": prompt, "options": options},
        answer_key={"correct_option_id": correct_option_id},
        scoring={"max_score": max_score},
    )
    published_question = publish_question_version(question_version, question_registry)

    definition = ActivityDefinition.objects.create(name=activity_name, type_key="assessment")
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
