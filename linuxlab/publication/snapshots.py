import hashlib
import json
import uuid

import psycopg
from django.db import OperationalError, models, transaction

from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.models import PublicationSnapshot
from linuxlab.publication.validators import validate_captured_learning_path

_MAX_DEADLOCK_RETRIES = 3


def canonical_json_bytes(payload):
    """Deterministic canonical serialization for hashing: sorted keys,
    compact separators, UTF-8. The same semantic payload always produces
    the same bytes regardless of construction/dict-iteration order.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def compute_content_hash(payload):
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _serialize_block(block):
    if block.block_type == "heading":
        return {"type": "heading", "value": str(block.value)}
    if block.block_type == "text":
        return {"type": "text", "value": str(block.value)}
    if block.block_type == "command":
        return {
            "type": "command",
            "value": {
                "command": block.value["command"],
                "expected_output": block.value["expected_output"],
            },
        }
    if block.block_type == "tip":
        return {
            "type": "tip",
            "value": {
                "heading": block.value["heading"],
                "body": str(block.value["body"]),
            },
        }
    if block.block_type == "activity":
        return {
            "type": "activity",
            "value": {
                "activity_public_id": str(block.value["activity_public_id"]),
                "activity_version": block.value["activity_version"],
            },
        }
    raise ValueError(f"unknown block type '{block.block_type}'")


def _serialize_lesson(lesson):
    return {
        "public_id": str(lesson.public_id),
        "title": lesson.title,
        "blocks": [_serialize_block(block) for block in lesson.body],
    }


def capture_learning_path(learning_path_page, *, _on_locked=None):
    """Lock and read the entire LearningPathPage -> ModulePage -> LessonPage
    subtree as ONE coherent authoring state, and return it as a plain,
    JSON-safe, immutable-by-convention structure (ADR-014 corrective:
    Criticals 2/3). This structure IS the eventual publication payload --
    nothing downstream re-reads Wagtail.

    Locking every row in the subtree with SELECT FOR UPDATE -- not just the
    root -- is what actually conflicts with normal Wagtail author writes:
    Wagtail's save_revision() and revision.publish() both end with a plain
    object.save() against the live page (an UPDATE hitting exactly these
    rows, across both the base wagtailcore_page table and the concrete
    Module/LessonPage table via Django's multi-table-inheritance save), and
    add_child()/delete() update the parent's numchild -- so every semantic
    edit and every membership change (add/remove Module or Lesson) in this
    subtree blocks until this transaction commits or rolls back. Confirmed
    empirically: a concurrent save_revision()+publish() blocks with
    wait_event_type='Lock' the instant it reaches this subtree, and only
    proceeds once this transaction ends.

    Rows are locked top-down (root, then modules, then lessons, each
    ordered by path) in the same fixed order on every call, so two
    concurrent captures of the same source never deadlock against each
    other on lock order -- they simply queue on the root lock, which is
    also what gives publish_learning_path() its capture-before-version
    ordering (Correction C: whichever caller acquires the root lock first
    runs its entire capture/validate/hash/version/insert/commit before the
    next can even begin capturing). A transient deadlock IS still possible
    between a publisher and an unrelated concurrent author transaction that
    reaches a shared leaf row from a different direction; that is handled
    by retrying the whole publish_learning_path() transaction, not by
    locking differently here.

    The caller's `learning_path_page` object may be stale; only its pk is
    trusted -- every field below is read from the row just locked, never
    from the caller's in-memory instance (ADR-014 corrective: Task 09 must
    not trust stale caller state as publication source).

    Must be called inside an open transaction.atomic() block if the caller
    needs the locks to outlive this call (as publish_learning_path() does);
    called standalone, this opens and commits its own transaction, so the
    coherence guarantee only covers the capture itself.
    """
    with transaction.atomic():
        locked_path = LearningPathPage.objects.select_for_update().get(pk=learning_path_page.pk)

        locked_modules = list(
            ModulePage.objects.select_for_update()
            .child_of(locked_path)
            .specific()
            .order_by("path")
        )
        captured_modules = []
        for module in locked_modules:
            locked_lessons = list(
                LessonPage.objects.select_for_update()
                .child_of(module)
                .specific()
                .order_by("path")
            )
            captured_modules.append(
                {
                    "public_id": str(module.public_id),
                    "title": module.title,
                    "lessons": [_serialize_lesson(lesson) for lesson in locked_lessons],
                }
            )

        capture = {
            "kind": "learning_path",
            "learning_path": {
                "public_id": str(locked_path.public_id),
                "title": locked_path.title,
                "modules": captured_modules,
            },
        }

        if _on_locked is not None:
            # Test-only synchronization seam: every production caller passes
            # None. It exists so a concurrency test can deterministically
            # observe/pause at the exact moment every row in the subtree is
            # locked but nothing has been validated or persisted yet --
            # without it, proving Criticals 2/3 and Correction C requires
            # either sleeps (banned, inherently racy) or reaching into
            # Django/psycopg internals from the test. It never bypasses any
            # invariant; it only controls timing.
            _on_locked()

        return capture


def _insert_published_snapshot(*, kind, source_public_id, version, payload, content_hash):
    """The sole controlled insertion path for PublicationSnapshot rows
    (ADR-014, Task 09 corrective Critical 1). PublicationSnapshot.save()
    unconditionally rejects and its manager blocks bulk_create()/update(),
    so ordinary application code cannot create or mutate a row at all. This
    function is private to the publication implementation and is only ever
    called from here, inside publish_learning_path()'s locked transaction,
    strictly after dependency validation, coherent capture, deterministic
    hashing, and version allocation have all already passed -- so every row
    it inserts is trustworthy by construction. It bypasses the blocked
    save() by calling Django's underlying Model.save_base() directly (which
    save() itself would normally call, had it not been overridden) -- the
    same bypass spirit as Task 07's controlled QuerySet.update() for
    Attempt.
    """
    instance = PublicationSnapshot(
        kind=kind,
        source_public_id=source_public_id,
        version=version,
        payload=payload,
        content_hash=content_hash,
    )
    models.Model.save_base(instance, force_insert=True)
    return instance


def _is_transient_deadlock(exc):
    return isinstance(exc, OperationalError) and isinstance(exc.__cause__, psycopg.errors.DeadlockDetected)


def publish_learning_path(learning_path_page, activity_registry, *, _on_captured=None):
    """Validate dependencies against, and persist, ONE coherently captured
    authoring state, and allocate the next immutable publication version for
    this LearningPathPage (ADR-014).

    Capture, dependency validation, canonicalization/hashing, version
    allocation, and insertion all happen on the SAME captured structure
    inside ONE transaction (Task 09 corrective, Critical 2/3, Correction C):
    capture_learning_path() locks the whole subtree before returning it, so
    no author edit -- semantic or tree-membership -- can land in between
    validating and persisting, and no concurrent publisher of the same
    source can allocate a version before this one has captured. Fails
    closed: validate_captured_learning_path() raises before anything is
    hashed or written if any dependency is missing/unpublished/malformed,
    including a transitive QuestionVersion an already-published
    ActivityVersion depends on (Correction D).

    A genuine deadlock between this transaction and an unrelated concurrent
    author transaction reaching a shared row from a different lock order is
    possible (confirmed empirically) even though same-source publishers
    never deadlock against each other; PostgreSQL's deadlock detector always
    resolves it by aborting one side, which this retries as a whole from
    scratch, bounded, since retrying is safe -- nothing was persisted.
    ValueError (a real dependency/validation failure) is never retried.
    """
    for attempt in range(1, _MAX_DEADLOCK_RETRIES + 1):
        try:
            with transaction.atomic():
                capture = capture_learning_path(learning_path_page, _on_locked=_on_captured)

                validate_captured_learning_path(capture, activity_registry)

                content_hash = compute_content_hash(capture)
                source_public_id = uuid.UUID(capture["learning_path"]["public_id"])

                current_max = (
                    PublicationSnapshot.objects.filter(
                        kind="learning_path", source_public_id=source_public_id
                    )
                    .order_by("-version")
                    .values_list("version", flat=True)
                    .first()
                )
                next_version = (current_max or 0) + 1

                return _insert_published_snapshot(
                    kind="learning_path",
                    source_public_id=source_public_id,
                    version=next_version,
                    payload=capture,
                    content_hash=content_hash,
                )
        except OperationalError as exc:
            if _is_transient_deadlock(exc) and attempt < _MAX_DEADLOCK_RETRIES:
                continue
            raise
