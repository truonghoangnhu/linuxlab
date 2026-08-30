"""Task 14 persistence: NodeRegistration, OutboxMessage, InboxMessage.

These reuse the Task 12 ImmutableArtifact trust-boundary pattern
(linuxlab/sync/models.py): ordinary create/save/bulk_create/update/
bulk_update/delete are blocked outright; the only legitimate writers are
private trusted helpers in the owning service modules (enqueue.py,
outbox.py, ingestion.py, processing.py, authentication.py)."""

import uuid
from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, transaction

from linuxlab.sync.models import (
    INBOX_FAILED,
    INBOX_PROCESSED,
    INBOX_RECEIVED,
    OUTBOX_DEAD_LETTER,
    OUTBOX_DELIVERED,
    OUTBOX_IN_FLIGHT,
    OUTBOX_PENDING,
    InboxMessage,
    NodeRegistration,
    OutboxMessage,
    _insert_immutable_row,
    _write_immutable_row_fields,
)

pytestmark = pytest.mark.django_db


def _make_node_registration(**overrides):
    fields = {
        "node_id": uuid.uuid4(),
        "display_name": "Lab A",
        "current_secret_hash": "hash-1",
        "previous_secret_hash": "",
        "previous_valid_until": None,
        "enabled": True,
    }
    fields.update(overrides)
    return _insert_immutable_row(NodeRegistration, **fields)


def _make_outbox_message(**overrides):
    fields = {
        "event_id": uuid.uuid4(),
        "event_type": "activity.completed",
        "schema_version": 1,
        "source_node_id": uuid.uuid4(),
        "occurred_at": datetime.now(UTC),
        "aggregate_type": "attempt",
        "aggregate_public_id": uuid.uuid4(),
        "payload": {"score": 1, "max_score": 1},
        "state": OUTBOX_PENDING,
        "next_attempt_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return _insert_immutable_row(OutboxMessage, **fields)


def _make_inbox_message(**overrides):
    node = overrides.pop("source_node", None) or _make_node_registration()
    fields = {
        "event_id": uuid.uuid4(),
        "source_node": node,
        "event_type": "activity.completed",
        "schema_version": 1,
        "occurred_at": datetime.now(UTC),
        "aggregate_type": "attempt",
        "aggregate_public_id": uuid.uuid4(),
        "payload": {"score": 1, "max_score": 1},
        "processing_state": INBOX_RECEIVED,
    }
    fields.update(overrides)
    return _insert_immutable_row(InboxMessage, **fields)


# --- NodeRegistration --------------------------------------------------------


def test_node_registration_node_id_is_unique():
    node_id = uuid.uuid4()
    _make_node_registration(node_id=node_id)

    with pytest.raises(IntegrityError), transaction.atomic():
        _make_node_registration(node_id=node_id)


def test_node_registration_ordinary_create_is_blocked():
    with pytest.raises(ValueError, match="cannot be created"):
        NodeRegistration.objects.create(
            node_id=uuid.uuid4(),
            display_name="x",
            current_secret_hash="h",
        )


def test_node_registration_ordinary_save_update_is_blocked():
    node = _make_node_registration()
    node.enabled = False
    with pytest.raises(ValueError, match="cannot be created or modified"):
        node.save()


def test_node_registration_ordinary_delete_is_blocked():
    node = _make_node_registration()
    with pytest.raises(ValueError, match="cannot be deleted"):
        node.delete()


def test_write_immutable_row_fields_updates_only_allowed_node_registration_fields():
    node = _make_node_registration(enabled=True)

    _write_immutable_row_fields(node, enabled=False)

    assert NodeRegistration.objects.get(pk=node.pk).enabled is False


def test_write_immutable_row_fields_rejects_non_allowlisted_field():
    node = _make_node_registration()

    with pytest.raises(ValueError, match="non-mutable"):
        _write_immutable_row_fields(node, display_name="hacked")


# --- corrective (independent review finding #2): the allow-list is fixed
# inside the trust boundary, keyed by model class -- a caller can no longer
# pass its own allowed_fields, not even to (deliberately or accidentally)
# widen it. Every protected immutable field is individually probed, and the
# legacy 4-positional-argument call shape is proven impossible outright.


def test_old_caller_supplied_allow_list_signature_is_no_longer_callable():
    node = _make_node_registration(enabled=True)

    with pytest.raises(TypeError):
        _write_immutable_row_fields(NodeRegistration, node.pk, {"node_id"}, node_id=uuid.uuid4())

    assert NodeRegistration.objects.get(pk=node.pk).node_id == node.node_id


@pytest.mark.parametrize(
    "field_name,new_value",
    [
        ("node_id", uuid.uuid4()),
        ("display_name", "hacked"),
        ("created_at", datetime.now(UTC)),
    ],
)
def test_node_registration_immutable_fields_are_all_individually_rejected(field_name, new_value):
    node = _make_node_registration()
    original = getattr(node, field_name)

    with pytest.raises(ValueError, match="non-mutable"):
        _write_immutable_row_fields(node, **{field_name: new_value})

    assert getattr(NodeRegistration.objects.get(pk=node.pk), field_name) == original


def test_node_registration_legitimate_rotation_metadata_still_works():
    node = _make_node_registration()
    now = datetime.now(UTC)

    _write_immutable_row_fields(
        node,
        current_secret_hash="new-hash",
        previous_secret_hash="old-hash",
        previous_valid_until=now,
        rotated_at=now,
    )

    reloaded = NodeRegistration.objects.get(pk=node.pk)
    assert reloaded.current_secret_hash == "new-hash"
    assert reloaded.previous_secret_hash == "old-hash"
    assert reloaded.rotated_at == now


def test_node_registration_legitimate_revoke_metadata_still_works():
    node = _make_node_registration()
    now = datetime.now(UTC)

    _write_immutable_row_fields(node, enabled=False, revoked_at=now)

    reloaded = NodeRegistration.objects.get(pk=node.pk)
    assert reloaded.enabled is False
    assert reloaded.revoked_at == now


# --- OutboxMessage -----------------------------------------------------------


def test_outbox_event_id_is_unique():
    event_id = uuid.uuid4()
    _make_outbox_message(event_id=event_id)

    with pytest.raises(IntegrityError), transaction.atomic():
        _make_outbox_message(event_id=event_id)


def test_outbox_state_check_constraint_rejects_invalid_value():
    with pytest.raises(IntegrityError), transaction.atomic():
        _make_outbox_message(state="not-a-real-state")


def test_outbox_state_allows_all_four_documented_values():
    for state in (OUTBOX_PENDING, OUTBOX_IN_FLIGHT, OUTBOX_DELIVERED, OUTBOX_DEAD_LETTER):
        _make_outbox_message(event_id=uuid.uuid4(), state=state)
    assert OutboxMessage.objects.count() == 4


def test_outbox_ordinary_create_is_blocked():
    with pytest.raises(ValueError, match="cannot be created"):
        OutboxMessage.objects.create(
            event_id=uuid.uuid4(),
            event_type="activity.completed",
            schema_version=1,
            source_node_id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            aggregate_type="attempt",
            aggregate_public_id=uuid.uuid4(),
            payload={},
        )


def test_outbox_ordinary_save_cannot_rewrite_envelope():
    outbox = _make_outbox_message()
    outbox.payload = {"tampered": True}
    with pytest.raises(ValueError, match="cannot be created or modified"):
        outbox.save()

    assert OutboxMessage.objects.get(pk=outbox.pk).payload == {"score": 1, "max_score": 1}


def test_outbox_queryset_update_is_blocked():
    outbox = _make_outbox_message()
    with pytest.raises(ValueError, match="immutable"):
        OutboxMessage.objects.filter(pk=outbox.pk).update(state=OUTBOX_DELIVERED)


def test_outbox_ordinary_delete_is_blocked():
    outbox = _make_outbox_message()
    with pytest.raises(ValueError, match="cannot be deleted"):
        outbox.delete()


def test_write_immutable_row_fields_updates_only_allowed_outbox_delivery_fields():
    outbox = _make_outbox_message()

    _write_immutable_row_fields(outbox, state=OUTBOX_IN_FLIGHT, attempts=1)

    reloaded = OutboxMessage.objects.get(pk=outbox.pk)
    assert reloaded.state == OUTBOX_IN_FLIGHT
    assert reloaded.attempts == 1
    assert reloaded.payload == {"score": 1, "max_score": 1}


def test_write_immutable_row_fields_rejects_envelope_field_for_outbox():
    outbox = _make_outbox_message()

    with pytest.raises(ValueError, match="non-mutable"):
        _write_immutable_row_fields(outbox, payload={"tampered": True})


@pytest.mark.parametrize(
    "field_name,new_value",
    [
        ("event_id", uuid.uuid4()),
        ("event_type", "hacked.event"),
        ("schema_version", 999),
        ("source_node_id", uuid.uuid4()),
        ("occurred_at", datetime.now(UTC)),
        ("aggregate_type", "hacked"),
        ("aggregate_public_id", uuid.uuid4()),
        ("payload", {"tampered": True}),
        ("created_at", datetime.now(UTC)),
    ],
)
def test_outbox_immutable_envelope_fields_are_all_individually_rejected(field_name, new_value):
    outbox = _make_outbox_message()
    original = getattr(outbox, field_name)

    with pytest.raises(ValueError, match="non-mutable"):
        _write_immutable_row_fields(outbox, **{field_name: new_value})

    assert getattr(OutboxMessage.objects.get(pk=outbox.pk), field_name) == original


def test_outbox_legitimate_delivery_metadata_still_works_via_bulk_writer():
    from linuxlab.sync.models import _bulk_write_immutable_rows

    outbox = _make_outbox_message()
    now = datetime.now(UTC)

    updated = _bulk_write_immutable_rows(
        OutboxMessage.objects.filter(pk=outbox.pk), state=OUTBOX_DELIVERED, delivered_at=now
    )

    assert updated == 1
    reloaded = OutboxMessage.objects.get(pk=outbox.pk)
    assert reloaded.state == OUTBOX_DELIVERED
    assert reloaded.delivered_at == now


def test_bulk_write_immutable_rows_rejects_non_mutable_outbox_field():
    from linuxlab.sync.models import _bulk_write_immutable_rows

    outbox = _make_outbox_message()

    with pytest.raises(ValueError, match="non-mutable"):
        _bulk_write_immutable_rows(
            OutboxMessage.objects.filter(pk=outbox.pk), payload={"tampered": True}
        )

    assert OutboxMessage.objects.get(pk=outbox.pk).payload == {"score": 1, "max_score": 1}


# --- InboxMessage ------------------------------------------------------------


def test_inbox_event_id_is_unique():
    event_id = uuid.uuid4()
    _make_inbox_message(event_id=event_id)

    with pytest.raises(IntegrityError), transaction.atomic():
        _make_inbox_message(event_id=event_id)


def test_inbox_processing_state_check_constraint_rejects_invalid_value():
    with pytest.raises(IntegrityError), transaction.atomic():
        _make_inbox_message(processing_state="not-a-real-state")


def test_inbox_processing_state_allows_all_three_documented_values():
    for state in (INBOX_RECEIVED, INBOX_PROCESSED, INBOX_FAILED):
        _make_inbox_message(event_id=uuid.uuid4(), processing_state=state)
    assert InboxMessage.objects.count() == 3


def test_inbox_ordinary_create_is_blocked():
    node = _make_node_registration()
    with pytest.raises(ValueError, match="cannot be created"):
        InboxMessage.objects.create(
            event_id=uuid.uuid4(),
            source_node=node,
            event_type="activity.completed",
            schema_version=1,
            occurred_at=datetime.now(UTC),
            aggregate_type="attempt",
            aggregate_public_id=uuid.uuid4(),
            payload={},
        )


def test_inbox_ordinary_save_cannot_rewrite_envelope():
    inbox = _make_inbox_message()
    inbox.payload = {"tampered": True}
    with pytest.raises(ValueError, match="cannot be created or modified"):
        inbox.save()

    assert InboxMessage.objects.get(pk=inbox.pk).payload == {"score": 1, "max_score": 1}


def test_inbox_queryset_update_is_blocked():
    inbox = _make_inbox_message()
    with pytest.raises(ValueError, match="immutable"):
        InboxMessage.objects.filter(pk=inbox.pk).update(processing_state=INBOX_PROCESSED)


def test_inbox_ordinary_delete_is_blocked():
    inbox = _make_inbox_message()
    with pytest.raises(ValueError, match="cannot be deleted"):
        inbox.delete()


def test_write_immutable_row_fields_updates_only_allowed_inbox_processing_fields():
    inbox = _make_inbox_message()

    _write_immutable_row_fields(inbox, processing_state=INBOX_PROCESSED)

    reloaded = InboxMessage.objects.get(pk=inbox.pk)
    assert reloaded.processing_state == INBOX_PROCESSED
    assert reloaded.payload == {"score": 1, "max_score": 1}


@pytest.mark.parametrize(
    "field_name,new_value",
    [
        ("event_id", uuid.uuid4()),
        ("event_type", "hacked.event"),
        ("schema_version", 999),
        ("occurred_at", datetime.now(UTC)),
        ("aggregate_type", "hacked"),
        ("aggregate_public_id", uuid.uuid4()),
        ("payload", {"tampered": True}),
    ],
)
def test_inbox_immutable_envelope_fields_are_all_individually_rejected(field_name, new_value):
    inbox = _make_inbox_message()
    original = getattr(inbox, field_name)

    with pytest.raises(ValueError, match="non-mutable"):
        _write_immutable_row_fields(inbox, **{field_name: new_value})

    assert getattr(InboxMessage.objects.get(pk=inbox.pk), field_name) == original


def test_inbox_source_node_is_rejected_as_non_mutable():
    node = _make_node_registration()
    other_node = _make_node_registration()
    inbox = _make_inbox_message(source_node=node)

    with pytest.raises(ValueError, match="non-mutable"):
        _write_immutable_row_fields(inbox, source_node=other_node)

    assert InboxMessage.objects.get(pk=inbox.pk).source_node_id == node.pk


def test_inbox_legitimate_processing_metadata_still_works():
    inbox = _make_inbox_message()

    _write_immutable_row_fields(inbox, processing_state=INBOX_FAILED, processing_error="boom")

    reloaded = InboxMessage.objects.get(pk=inbox.pk)
    assert reloaded.processing_state == INBOX_FAILED
    assert reloaded.processing_error == "boom"


# --- historical Task 12 rows remain readable --------------------------------


def test_historical_task12_installed_package_still_readable():
    from linuxlab.sync.models import InstalledPackage

    assert list(InstalledPackage.objects.all()) == []
    assert InstalledPackage.objects.filter(package_type="learning_path").count() == 0
