import hashlib
import json
import uuid

from django.db import connection, models, transaction

from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.models import PublicationSnapshot
from linuxlab.publication.validators import validate_captured_learning_path


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


def _set_repeatable_read():
    """Must be the FIRST statement of the current transaction -- PostgreSQL
    rejects SET TRANSACTION ISOLATION LEVEL once any other statement has
    run in it. Confirmed empirically that entering transaction.atomic()
    itself issues only BEGIN, never a query, so calling this immediately
    after `with transaction.atomic():` is safe.
    """
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")


def capture_learning_path(learning_path_page, *, _on_snapshot_started=None, _on_captured=None):
    """Read the entire LearningPathPage -> ModulePage -> LessonPage subtree
    as one coherent authoring state using PLAIN reads -- no SELECT FOR
    UPDATE -- and return it as a plain, JSON-safe, immutable-by-convention
    structure. This structure IS the eventual publication payload; nothing
    downstream re-reads Wagtail.

    Must be called inside a transaction already running at REPEATABLE READ
    (see _set_repeatable_read()): PostgreSQL fixes that transaction's MVCC
    snapshot at its first statement -- which is the first query this
    function issues below -- and every later query in the SAME transaction,
    however much later, keeps observing that exact snapshot. Confirmed
    empirically: a concurrent author transaction that changes multiple rows
    (e.g. a Module's title AND a Lesson's body together) and commits AFTER
    this function's first query but before its later ones is invisible to
    all of them -- they still see the pre-commit state, coherently, with no
    row lock anywhere. This is what gives Task 09 both a torn-free capture
    and zero interference with ordinary Wagtail author writes: authors are
    never blocked, because nothing here ever locks a row they touch.

    The caller's `learning_path_page` object may be stale; only its pk is
    trusted -- every field below is read from the row this function itself
    resolves, never from the caller's in-memory instance (ADR-014
    correction: publication must not trust stale caller state as its
    source).
    """
    path = LearningPathPage.objects.get(pk=learning_path_page.pk)

    if _on_snapshot_started is not None:
        # Test-only synchronization seam, never used by production callers
        # (always None): fires the instant this transaction's REPEATABLE
        # READ snapshot is fixed (right after its first query), so a
        # concurrency test can deterministically let an author edit commit
        # AFTER that point and prove it is invisible to this capture --
        # without reaching into psycopg/Django internals or sleeping. It
        # exposes no mutable state and cannot skip validation, replace the
        # hash, or insert anything; it only controls timing.
        _on_snapshot_started()

    modules = list(
        ModulePage.objects.child_of(path).specific().order_by("path"),
    )
    captured_modules = []
    for module in modules:
        lessons = list(LessonPage.objects.child_of(module).specific().order_by("path"))
        captured_modules.append(
            {
                "public_id": str(module.public_id),
                "title": module.title,
                "lessons": [_serialize_lesson(lesson) for lesson in lessons],
            }
        )

    capture = {
        "kind": "learning_path",
        "learning_path": {
            "public_id": str(path.public_id),
            "title": path.title,
            "modules": captured_modules,
        },
    }

    if _on_captured is not None:
        # Test-only synchronization seam (see above): fires once every read
        # is done, before validation/hashing/persistence, for tests that
        # need to inspect or pause on the finished capture itself.
        _on_captured()

    return capture


def _insert_published_snapshot(*, kind, source_public_id, version, payload, content_hash):
    """The sole controlled insertion path for PublicationSnapshot rows
    (ADR-014, Task 09 corrective Critical 1). PublicationSnapshot.save()
    unconditionally rejects, and that guard is bound to EVERY manager
    Django exposes (Meta.base_manager_name), which also blocks
    bulk_create()/update()/bulk_update() everywhere, so ordinary
    application code cannot create or mutate a row at all. This function is
    private to the publication implementation and is only ever called from
    here, inside publish_learning_path()'s own transaction, strictly after
    dependency validation, coherent capture, deterministic hashing, and
    version allocation have all already passed -- so every row it inserts
    is trustworthy by construction. It bypasses the blocked save() by
    calling Django's underlying Model.save_base() directly (which save()
    itself would normally call, had it not been overridden) -- the same
    bypass spirit as Task 07's controlled QuerySet.update() for Attempt.
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


def _publication_mutex_key(kind, source_public_id):
    return f"publication:{kind}:{source_public_id}"


def _acquire_publication_mutex(kind, source_public_id):
    """A PostgreSQL SESSION-level advisory lock (not pg_advisory_XACT_lock)
    is deliberately used as the ONLY publisher-vs-publisher serialization
    primitive (Task 09 final correction): it must be held from before this
    transaction begins until after it commits, spanning the mutex
    acquisition (in autocommit, on this connection) and the whole
    REPEATABLE READ transaction that follows -- an xact-scoped lock would
    release at COMMIT, too early to keep a second publisher from starting
    its own snapshot before this one's insert is visible. It is
    intentionally NOT a row lock: ordinary Wagtail authors never touch it,
    so it cannot block or deadlock against them (confirmed empirically).
    hashtextextended() is PostgreSQL's own deterministic 64-bit text hash,
    computed server-side -- never Python's hash(), whose per-process salt
    (PYTHONHASHSEED) makes it unstable across the very processes/workers
    that would need to agree on the same lock key.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            [_publication_mutex_key(kind, source_public_id)],
        )


def _release_publication_mutex(kind, source_public_id):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            [_publication_mutex_key(kind, source_public_id)],
        )


def publish_learning_path(
    learning_path_page, activity_registry, *, _on_snapshot_started=None, _on_captured=None
):
    """Validate dependencies against, and persist, ONE coherently captured
    authoring state, and allocate the next immutable publication version for
    this LearningPathPage (ADR-014).

    Publisher-vs-publisher ordering and the coherent authoring snapshot are
    two SEPARATE concerns handled by two separate mechanisms (Task 09 final
    correction): a PostgreSQL session-level advisory lock, keyed on
    (kind, source_public_id) and acquired BEFORE this transaction begins,
    is the ONLY thing that serializes two publishers of the SAME source --
    it is never touched by Wagtail authors. Coherence of the captured tree
    comes entirely from PostgreSQL's own REPEATABLE READ MVCC snapshot
    (fixed at capture_learning_path()'s first query), not from locking any
    authoring row -- so ordinary Wagtail writes (edit, add_child, move,
    delete) are never blocked by a publication in progress, and no
    publisher-vs-author deadlock is possible any more (there is nothing
    left for the two sides to contend over). The advisory lock must be
    acquired strictly before the REPEATABLE READ transaction opens: that
    transaction's snapshot is fixed at ITS first query, so if the lock were
    taken any later, a second publisher could establish its snapshot before
    a first publisher (that got the lock later) actually inserts, breaking
    the version-vs-content-order guarantee below.

    Capture, dependency validation, canonicalization/hashing, version
    allocation, and insertion all happen on the SAME captured structure
    inside the mutex-protected transaction (Critical 2/3, Correction C): no
    author edit can land in between validating and persisting from this
    transaction's point of view (MVCC hides it), and no concurrent
    publisher of the same source can even begin its own capture until this
    one has committed and released the mutex -- so whichever publisher
    acquires the mutex first is guaranteed the lower version AND the
    earlier authoring state. Fails closed: validate_captured_learning_path()
    raises before anything is hashed or written if any dependency is
    missing/unpublished/malformed, including a transitive QuestionVersion an
    already-published ActivityVersion depends on (Correction D).

    The mutex is released in `finally` regardless of how this function
    exits (validation failure, DB error, or success), so a prior failed
    call can never leave a later publisher of the same source hanging.
    """
    kind = "learning_path"
    source_public_id = learning_path_page.public_id

    _acquire_publication_mutex(kind, source_public_id)
    try:
        with transaction.atomic():
            _set_repeatable_read()

            capture = capture_learning_path(
                learning_path_page,
                _on_snapshot_started=_on_snapshot_started,
                _on_captured=_on_captured,
            )

            validate_captured_learning_path(capture, activity_registry)

            content_hash = compute_content_hash(capture)
            resolved_source_public_id = uuid.UUID(capture["learning_path"]["public_id"])

            current_max = (
                PublicationSnapshot.objects.filter(
                    kind=kind, source_public_id=resolved_source_public_id
                )
                .order_by("-version")
                .values_list("version", flat=True)
                .first()
            )
            next_version = (current_max or 0) + 1

            return _insert_published_snapshot(
                kind=kind,
                source_public_id=resolved_source_public_id,
                version=next_version,
                payload=capture,
                content_hash=content_hash,
            )
    finally:
        _release_publication_mutex(kind, source_public_id)
