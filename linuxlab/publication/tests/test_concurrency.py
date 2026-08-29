"""Task 09 corrective concurrency evidence (Criticals 2/3, Corrections C/D).

Every test here uses REAL PostgreSQL row-lock blocking (via
capture_learning_path()'s `_on_locked` test seam and _pg_sync's
pg_stat_activity polling) to drive a deterministic race, never sleeps.
"""

import threading

import pytest
from django.db import close_old_connections, connection, transaction

from linuxlab.activities.models import ActivityDefinition
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.models import PublicationSnapshot
from linuxlab.publication.snapshots import publish_learning_path

from ._pg_sync import wait_until_a_backend_is_lock_blocked, wait_until_n_backends_are_lock_blocked
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


# --- Critical 2: validation/build TOCTOU ------------------------------


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

    locked_event = threading.Event()
    proceed_event = threading.Event()
    publish_result = {}

    def on_locked():
        locked_event.set()
        proceed_event.wait(timeout=10)

    def publisher():
        close_old_connections()
        try:
            publish_result["snapshot"] = publish_learning_path(
                path, registry, _on_captured=on_locked
            )
        finally:
            connection.close()

    def author():
        close_old_connections()
        try:
            with transaction.atomic():
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
        finally:
            connection.close()

    t_pub = threading.Thread(target=publisher)
    t_pub.start()
    assert locked_event.wait(timeout=10)

    t_author = threading.Thread(target=author)
    t_author.start()
    wait_until_a_backend_is_lock_blocked()

    proceed_event.set()
    t_pub.join(timeout=10)
    t_author.join(timeout=10)

    # Design guarantee: capture_learning_path() locks every row in the
    # subtree before returning, so the author's edit cannot land until the
    # publisher's transaction ends -- the publisher deterministically
    # validates AND persists the still-old (still-published) reference
    # (acceptable outcome A, Task 09 corrective section 16). What must never
    # happen -- validation observing v_old but the persisted payload
    # containing v_new -- cannot happen here because both read the SAME
    # captured structure.
    assert "error" not in publish_result
    snapshot = publish_result["snapshot"]
    ref = snapshot.payload["learning_path"]["modules"][0]["lessons"][0]["blocks"][0]["value"]
    assert ref["activity_version"] == old_version.version
    assert PublicationSnapshot.objects.filter(source_public_id=path.public_id).count() == 1


# --- Critical 3: torn multi-row authoring state -------------------------


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

    locked_event = threading.Event()
    proceed_event = threading.Event()
    publish_result = {}

    def on_locked():
        locked_event.set()
        proceed_event.wait(timeout=10)

    def publisher():
        close_old_connections()
        try:
            publish_result["snapshot"] = publish_learning_path(
                path, registry, _on_captured=on_locked
            )
        finally:
            connection.close()

    def author():
        close_old_connections()
        try:
            with transaction.atomic():
                module_obj = ModulePage.objects.get(pk=module.pk)
                module_obj.title = "NEW module title"
                module_obj.save_revision().publish()

                lesson_obj = LessonPage.objects.get(pk=lesson.pk)
                lesson_obj.body = [("heading", "NEW lesson body")]
                lesson_obj.save_revision().publish()
        finally:
            connection.close()

    t_pub = threading.Thread(target=publisher)
    t_pub.start()
    assert locked_event.wait(timeout=10)

    t_author = threading.Thread(target=author)
    t_author.start()
    wait_until_a_backend_is_lock_blocked()

    proceed_event.set()
    t_pub.join(timeout=10)
    t_author.join(timeout=10)

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
    # Deterministic by this design (whole subtree locked before capture):
    # the author's two-row edit is one transaction and cannot land any part
    # of itself until the publisher, which locked first, has committed.
    assert old_pair
    assert not new_pair

    # The author's edit is not lost -- it applies once the publisher is done.
    reloaded_module = ModulePage.objects.get(pk=module.pk)
    reloaded_lesson = LessonPage.objects.get(pk=lesson.pk)
    assert reloaded_module.title == "NEW module title"
    assert reloaded_lesson.body[0].value == "NEW lesson body"


# --- tree membership (section 18): adding a Lesson participates in the lock


@pytest.mark.django_db(transaction=True)
def test_concurrent_lesson_addition_cannot_land_inside_a_capture_in_progress():
    registry = _registry()

    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [("heading", "OLD")]
    module.add_child(instance=lesson)

    locked_event = threading.Event()
    proceed_event = threading.Event()
    publish_result = {}

    def on_locked():
        locked_event.set()
        proceed_event.wait(timeout=10)

    def publisher():
        close_old_connections()
        try:
            publish_result["snapshot"] = publish_learning_path(
                path, registry, _on_captured=on_locked
            )
        finally:
            connection.close()

    def author_adds_a_lesson():
        close_old_connections()
        try:
            module_obj = ModulePage.objects.get(pk=module.pk)
            new_lesson = LessonPage(title="A brand new lesson")
            module_obj.add_child(instance=new_lesson)
        finally:
            connection.close()

    t_pub = threading.Thread(target=publisher)
    t_pub.start()
    assert locked_event.wait(timeout=10)

    t_author = threading.Thread(target=author_adds_a_lesson)
    t_author.start()
    wait_until_a_backend_is_lock_blocked()

    proceed_event.set()
    t_pub.join(timeout=10)
    t_author.join(timeout=10)

    # The publisher's capture -- taken before the addition could land --
    # must reflect the ORIGINAL single-lesson membership, never a
    # half-added second lesson.
    snapshot = publish_result["snapshot"]
    lessons_payload = snapshot.payload["learning_path"]["modules"][0]["lessons"]
    assert len(lessons_payload) == 1

    # The addition is not lost -- it applies once the publisher is done.
    assert LessonPage.objects.child_of(module).count() == 2


# --- Correction C: publisher chronology matches actual capture order ----


@pytest.mark.django_db(transaction=True)
def test_concurrent_publisher_chronology_matches_actual_capture_order():
    registry = _registry()

    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [("heading", "OLD")]
    module.add_child(instance=lesson)

    p1_locked = threading.Event()
    p1_proceed = threading.Event()
    p1_result = {}

    def on_p1_locked():
        p1_locked.set()
        p1_proceed.wait(timeout=10)

    def p1_worker():
        close_old_connections()
        try:
            p1_result["snapshot"] = publish_learning_path(path, registry, _on_captured=on_p1_locked)
        finally:
            connection.close()

    t1 = threading.Thread(target=p1_worker)
    t1.start()
    assert p1_locked.wait(timeout=10)

    def author_worker():
        close_old_connections()
        try:
            with transaction.atomic():
                lesson_obj = LessonPage.objects.get(pk=lesson.pk)
                lesson_obj.body = [("heading", "NEW")]
                lesson_obj.save_revision().publish()
        finally:
            connection.close()

    t_author = threading.Thread(target=author_worker)
    t_author.start()
    wait_until_a_backend_is_lock_blocked()

    p1_proceed.set()
    t1.join(timeout=10)

    # Sequenced, not raced: the second publisher only starts once the
    # author's edit has fully committed, so v2's capture order is settled
    # rather than left to chance against the author's own write -- both
    # would otherwise queue for the same leaf row (see
    # test_transient_deadlock_against_a_concurrent_author_transaction_is_retried_not_surfaced
    # for what happens when they genuinely do race).
    t_author.join(timeout=10)

    second = publish_learning_path(LearningPathPage.objects.get(pk=path.pk), registry)

    v1 = p1_result["snapshot"]
    assert v1.version == 1
    assert v1.payload["learning_path"]["modules"][0]["lessons"][0]["blocks"][0]["value"] == "OLD"

    assert second.version == 2
    assert second.payload["learning_path"]["modules"][0]["lessons"][0]["blocks"][0]["value"] == "NEW"


# --- section 21: transient deadlock is retried, not surfaced ------------


@pytest.mark.django_db(transaction=True)
def test_transient_deadlock_against_a_concurrent_author_transaction_is_retried_not_surfaced():
    registry = _registry()

    path = LearningPathPage(title="Linux Basics")
    _root().add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [("heading", "OLD")]
    module.add_child(instance=lesson)

    p1_locked = threading.Event()
    p1_proceed = threading.Event()
    p1_result = {}

    def on_p1_locked():
        p1_locked.set()
        p1_proceed.wait(timeout=10)

    def p1_worker():
        close_old_connections()
        try:
            p1_result["snapshot"] = publish_learning_path(path, registry, _on_captured=on_p1_locked)
        finally:
            connection.close()

    t1 = threading.Thread(target=p1_worker)
    t1.start()
    assert p1_locked.wait(timeout=10)

    author_result = {}

    def author_worker():
        # Unlike publish_learning_path(), an ordinary Wagtail author save has
        # no built-in retry -- that is out of Task 09's scope (publication
        # owns its own retry; a real editor would just retry the save, same
        # as with any concurrent write conflict). Either side of the two-way
        # deadlock below can be the one PostgreSQL aborts, so this thread
        # tolerates losing that race instead of asserting its own success --
        # what this test actually verifies is that the PUBLISHER'S retry
        # (in p2_worker) recovers.
        close_old_connections()
        try:
            with transaction.atomic():
                lesson_obj = LessonPage.objects.get(pk=lesson.pk)
                lesson_obj.body = [("heading", "NEW")]
                lesson_obj.save_revision().publish()
            author_result["committed"] = True
        except Exception as exc:  # noqa: BLE001
            author_result["error"] = exc
        finally:
            connection.close()

    t_author = threading.Thread(target=author_worker)
    t_author.start()
    wait_until_a_backend_is_lock_blocked()

    # Deliberately reproduce the transient deadlock: a second publisher
    # approaches the same leaf row (root -> module -> lesson) from a
    # different lock-acquisition path than the author's direct write, while
    # BOTH are queued behind P1's still-held lock (confirmed via a second
    # blocked-backend count, not a sleep).
    p2_result = {}

    def p2_worker():
        close_old_connections()
        try:
            reloaded_path = LearningPathPage.objects.get(pk=path.pk)
            p2_result["snapshot"] = publish_learning_path(reloaded_path, registry)
        except Exception as exc:  # noqa: BLE001
            p2_result["error"] = exc
        finally:
            connection.close()

    t2 = threading.Thread(target=p2_worker)
    t2.start()
    wait_until_n_backends_are_lock_blocked(n=2)

    p1_proceed.set()
    t1.join(timeout=10)
    t_author.join(timeout=10)
    t2.join(timeout=15)

    assert "error" not in p2_result, (
        f"a transient deadlock surfaced instead of being retried: {p2_result.get('error')!r}"
    )
    assert p1_result["snapshot"].version == 1
    assert p2_result["snapshot"].version == 2
    assert PublicationSnapshot.objects.filter(source_public_id=path.public_id).count() == 2
