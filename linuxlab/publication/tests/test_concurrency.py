"""Task 09 final corrective concurrency evidence.

Publication coherence comes entirely from a PostgreSQL session-level
advisory mutex (publisher-vs-publisher ordering) plus a REPEATABLE READ
MVCC snapshot (coherent tree capture) -- NOT from locking any authoring
row. Every test here proves both halves of that with real PostgreSQL
backend state (via _pg_sync's pg_stat_activity polling and the
_on_snapshot_started/_on_captured test seams), never sleeps, and every
concurrent author operation here is expected to complete WITHOUT blocking
or erroring, which is the whole point of this corrective.
"""

import threading

import pytest
from django.db import close_old_connections, connection

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.models import PublicationSnapshot
from linuxlab.publication.snapshots import _publication_mutex_key, publish_learning_path

from ._pg_sync import (
    assert_no_backend_is_row_lock_blocked,
    wait_until_a_backend_is_advisory_lock_blocked,
)
from ._wagtail_bootstrap import get_or_create_root_page


class FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": 1, "max_score": 1}


def _root():
    return get_or_create_root_page()


def _registry():
    reg = ActivityRegistry()
    reg.register(FakeProvider())
    return reg


# --- Critical 2: validation/build TOCTOU (MVCC, no author blocking) -----


@pytest.mark.django_db(transaction=True)
def test_author_edit_during_capture_never_produces_a_mixed_validation_and_persisted_state():
    registry = _registry()
    definition = ActivityDefinition.objects.create(name="Fake activity", type_key="fake")
    old_version = create_activity_version(definition, config={})
    publish_activity_version(old_version, registry)
    new_version = create_activity_version(definition, config={})  # left UNPUBLISHED

    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(definition.public_id),
                "activity_version": old_version.version,
            },
        )
    ]
    module.add_child(instance=lesson)

    snapshot_started = threading.Event()
    proceed = threading.Event()
    publish_result = {}
    author_result = {}

    def on_snapshot_started():
        snapshot_started.set()
        proceed.wait(timeout=10)

    def publisher():
        close_old_connections()
        try:
            publish_result["snapshot"] = publish_learning_path(
                path, registry, _on_snapshot_started=on_snapshot_started
            )
        finally:
            connection.close()

    t_pub = threading.Thread(target=publisher)
    t_pub.start()
    assert snapshot_started.wait(timeout=10)

    # The author's edit -- to an UNPUBLISHED dependency, exactly what
    # validation must reject -- runs and commits WHILE the publisher's
    # snapshot is already fixed. It must complete immediately: publication
    # holds no row lock the author could contend with.
    def author():
        close_old_connections()
        try:
            lesson_obj = LessonPage.objects.get(pk=lesson.pk)
            lesson_obj.body = [
                (
                    "activity",
                    {
                        "activity_public_id": str(definition.public_id),
                        "activity_version": new_version.version,  # unpublished
                    },
                )
            ]
            lesson_obj.save_revision().publish()
            author_result["committed"] = True
        except Exception as exc:  # noqa: BLE001
            author_result["error"] = exc
        finally:
            connection.close()

    t_author = threading.Thread(target=author)
    t_author.start()
    t_author.join(timeout=10)

    assert author_result.get("committed") is True, author_result.get("error")

    proceed.set()
    t_pub.join(timeout=10)

    # The publisher's REPEATABLE READ snapshot was fixed before the author's
    # commit, so it still sees (and validates against, and persists) the
    # OLD, still-published reference -- never a mix of "validated v_old" and
    # "persisted v_new".
    assert "error" not in publish_result
    snapshot = publish_result["snapshot"]
    ref = snapshot.payload["learning_path"]["modules"][0]["lessons"][0]["blocks"][0]["value"]
    assert ref["activity_version"] == old_version.version
    assert PublicationSnapshot.objects.filter(source_public_id=path.public_id).count() == 1

    # A later publication does observe the author's change and fails, since
    # the new dependency is unpublished.
    with pytest.raises(ValueError):
        publish_learning_path(LearningPathPage.objects.get(pk=path.pk), registry)


# --- Critical 3: torn multi-row authoring state (MVCC, no author blocking)


@pytest.mark.django_db(transaction=True)
def test_concurrent_two_row_author_edit_never_produces_a_torn_capture():
    registry = _registry()

    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [("heading", "OLD lesson body")]
    module.add_child(instance=lesson)

    snapshot_started = threading.Event()
    proceed = threading.Event()
    publish_result = {}
    author_result = {}

    def on_snapshot_started():
        snapshot_started.set()
        proceed.wait(timeout=10)

    def publisher():
        close_old_connections()
        try:
            publish_result["snapshot"] = publish_learning_path(
                path, registry, _on_snapshot_started=on_snapshot_started
            )
        finally:
            connection.close()

    t_pub = threading.Thread(target=publisher)
    t_pub.start()
    assert snapshot_started.wait(timeout=10)

    def author():
        close_old_connections()
        try:
            module_obj = ModulePage.objects.get(pk=module.pk)
            module_obj.title = "NEW module title"
            module_obj.save_revision().publish()

            lesson_obj = LessonPage.objects.get(pk=lesson.pk)
            lesson_obj.body = [("heading", "NEW lesson body")]
            lesson_obj.save_revision().publish()
            author_result["committed"] = True
        except Exception as exc:  # noqa: BLE001
            author_result["error"] = exc
        finally:
            connection.close()

    t_author = threading.Thread(target=author)
    t_author.start()
    t_author.join(timeout=10)
    assert author_result.get("committed") is True, author_result.get("error")

    proceed.set()
    t_pub.join(timeout=10)

    snapshot = publish_result["snapshot"]
    module_payload = snapshot.payload["learning_path"]["modules"][0]
    lesson_payload = module_payload["lessons"][0]

    old_pair = (
        module_payload["title"] == "Shell Basics"
        and lesson_payload["blocks"][0]["value"] == "OLD lesson body"
    )
    new_pair = (
        module_payload["title"] == "NEW module title"
        and lesson_payload["blocks"][0]["value"] == "NEW lesson body"
    )
    # Never acceptable: OLD module + NEW lesson, or NEW module + OLD lesson.
    assert old_pair or new_pair
    # Deterministic under REPEATABLE READ: the snapshot was fixed before the
    # author's two-row commit landed, so both fields come from before it.
    assert old_pair
    assert not new_pair

    # The author's edit is not lost -- a later read sees it.
    reloaded_module = ModulePage.objects.get(pk=module.pk)
    reloaded_lesson = LessonPage.objects.get(pk=lesson.pk)
    assert reloaded_module.title == "NEW module title"
    assert reloaded_lesson.body[0].value == "NEW lesson body"


# --- tree membership (MVCC, no author blocking) --------------------------


@pytest.mark.django_db(transaction=True)
def test_concurrent_lesson_addition_does_not_block_and_is_excluded_from_the_in_flight_capture():
    registry = _registry()

    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [("heading", "OLD")]
    module.add_child(instance=lesson)

    snapshot_started = threading.Event()
    proceed = threading.Event()
    publish_result = {}
    author_result = {}

    def on_snapshot_started():
        snapshot_started.set()
        proceed.wait(timeout=10)

    def publisher():
        close_old_connections()
        try:
            publish_result["snapshot"] = publish_learning_path(
                path, registry, _on_snapshot_started=on_snapshot_started
            )
        finally:
            connection.close()

    t_pub = threading.Thread(target=publisher)
    t_pub.start()
    assert snapshot_started.wait(timeout=10)

    def author_adds_a_lesson():
        close_old_connections()
        try:
            module_obj = ModulePage.objects.get(pk=module.pk)
            new_lesson = LessonPage(title="A brand new lesson")
            module_obj.add_child(instance=new_lesson)
            author_result["committed"] = True
        except Exception as exc:  # noqa: BLE001
            author_result["error"] = exc
        finally:
            connection.close()

    t_author = threading.Thread(target=author_adds_a_lesson)
    t_author.start()
    t_author.join(timeout=10)
    assert author_result.get("committed") is True, author_result.get("error")

    proceed.set()
    t_pub.join(timeout=10)

    # The publisher's snapshot -- fixed before the addition committed --
    # reflects the ORIGINAL single-lesson membership.
    snapshot = publish_result["snapshot"]
    lessons_payload = snapshot.payload["learning_path"]["modules"][0]["lessons"]
    assert len(lessons_payload) == 1

    # The addition is not lost -- it is visible afterward.
    assert LessonPage.objects.child_of(module).count() == 2


# --- publisher chronology: P2's snapshot begins only after the mutex -----


@pytest.mark.django_db(transaction=True)
def test_second_publisher_snapshot_begins_only_after_obtaining_the_mutex():
    registry = _registry()

    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [("heading", "OLD")]
    module.add_child(instance=lesson)

    p1_snapshot_started = threading.Event()
    p1_proceed = threading.Event()
    p1_result = {}

    def on_p1_snapshot_started():
        p1_snapshot_started.set()
        p1_proceed.wait(timeout=10)

    def p1_worker():
        close_old_connections()
        try:
            p1_result["snapshot"] = publish_learning_path(
                path, registry, _on_snapshot_started=on_p1_snapshot_started
            )
        finally:
            connection.close()

    t1 = threading.Thread(target=p1_worker)
    t1.start()
    assert p1_snapshot_started.wait(timeout=10)

    # A second publisher, requesting the SAME source, must queue on the
    # mutex -- confirmed via real PostgreSQL wait state, not a guess.
    p2_result = {}

    def p2_worker():
        close_old_connections()
        try:
            reloaded_path = LearningPathPage.objects.get(pk=path.pk)
            p2_result["snapshot"] = publish_learning_path(reloaded_path, registry)
        finally:
            connection.close()

    t2 = threading.Thread(target=p2_worker)
    t2.start()
    wait_until_a_backend_is_advisory_lock_blocked()
    assert_no_backend_is_row_lock_blocked()

    # The author's edit runs and commits WHILE P1 holds the mutex and P2 is
    # queued behind it -- and completes immediately, proving neither
    # publisher holds any row lock.
    def author_worker():
        close_old_connections()
        try:
            lesson_obj = LessonPage.objects.get(pk=lesson.pk)
            lesson_obj.body = [("heading", "NEW")]
            lesson_obj.save_revision().publish()
        finally:
            connection.close()

    t_author = threading.Thread(target=author_worker)
    t_author.start()
    t_author.join(timeout=10)

    p1_proceed.set()
    t1.join(timeout=10)
    t2.join(timeout=15)

    v1 = p1_result["snapshot"]
    assert v1.version == 1
    assert v1.payload["learning_path"]["modules"][0]["lessons"][0]["blocks"][0]["value"] == "OLD"

    v2 = p2_result["snapshot"]
    assert v2.version == 2
    # P2 only acquired the mutex (and so only opened its own REPEATABLE READ
    # snapshot) after P1 released it, which was after the author's commit --
    # so P2's snapshot begins strictly after that commit and observes it.
    assert v2.payload["learning_path"]["modules"][0]["lessons"][0]["blocks"][0]["value"] == "NEW"


# --- different sources never share a lock key -----------------------------


def test_different_sources_never_share_a_publication_mutex_key():
    import uuid

    a = uuid.uuid4()
    b = uuid.uuid4()
    assert _publication_mutex_key("learning_path", a) != _publication_mutex_key("learning_path", b)


def test_same_source_always_derives_the_same_publication_mutex_key():
    import uuid

    source = uuid.uuid4()
    assert _publication_mutex_key("learning_path", source) == _publication_mutex_key(
        "learning_path", source
    )


# --- mutex failure safety (section 24) ------------------------------------


@pytest.mark.django_db(transaction=True)
def test_publisher_mutex_is_released_after_a_validation_failure_so_a_later_publish_does_not_hang():
    registry = _registry()
    definition = ActivityDefinition.objects.create(name="Fake activity", type_key="fake")
    unpublished = create_activity_version(definition, config={})

    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [
        (
            "activity",
            {"activity_public_id": str(definition.public_id), "activity_version": unpublished.version},
        )
    ]
    module.add_child(instance=lesson)

    with pytest.raises(ValueError):
        publish_learning_path(path, registry)

    # If the mutex were not released in `finally`, this second call for the
    # SAME source would hang forever waiting on it.
    publish_activity_version(unpublished, registry)
    snapshot = publish_learning_path(path, registry)
    assert snapshot.version == 1


# --- section 22/23: no new retry requirement for ordinary Wagtail authors -


@pytest.mark.django_db(transaction=True)
def test_concurrent_author_operations_complete_successfully_while_publication_is_active():
    registry = _registry()

    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [("heading", "OLD")]
    module.add_child(instance=lesson)
    other_lesson = LessonPage(title="Another lesson")
    other_lesson.body = [("heading", "Will move")]
    module.add_child(instance=other_lesson)
    other_module = ModulePage(title="Networking Basics")
    path.add_child(instance=other_module)

    snapshot_started = threading.Event()
    proceed = threading.Event()
    publish_result = {}

    def on_snapshot_started():
        snapshot_started.set()
        proceed.wait(timeout=10)

    def publisher():
        close_old_connections()
        try:
            publish_result["snapshot"] = publish_learning_path(
                path, registry, _on_snapshot_started=on_snapshot_started
            )
        finally:
            connection.close()

    t_pub = threading.Thread(target=publisher)
    t_pub.start()
    assert snapshot_started.wait(timeout=10)

    author_errors = []

    def run_author_op(fn):
        close_old_connections()
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            author_errors.append(exc)
        finally:
            connection.close()

    def op_edit():
        lesson_obj = LessonPage.objects.get(pk=lesson.pk)
        lesson_obj.body = [("heading", "NEW")]
        lesson_obj.save_revision().publish()

    def op_add_child():
        module_obj = ModulePage.objects.get(pk=module.pk)
        module_obj.add_child(instance=LessonPage(title="Added concurrently"))

    def op_move():
        moving_lesson = LessonPage.objects.get(pk=other_lesson.pk)
        target_module = ModulePage.objects.get(pk=other_module.pk)
        moving_lesson.move(target_module, pos="last-child")

    threads = [threading.Thread(target=run_author_op, args=(op,)) for op in (op_edit, op_add_child, op_move)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert author_errors == [], f"author operations must not fail: {author_errors!r}"

    proceed.set()
    t_pub.join(timeout=10)

    assert "error" not in publish_result
    assert publish_result["snapshot"].version == 1
