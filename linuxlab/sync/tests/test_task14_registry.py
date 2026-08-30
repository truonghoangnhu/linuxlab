"""Task 14 fail-closed syncable-event registry: absence means LOCAL-ONLY /
unsupported by default; keyed by exact (event_type, schema_version)
(T14-D20, T14-D22)."""

import pytest

from linuxlab.sync.contracts import SyncEventContract
from linuxlab.sync.registry import get_sync_event_contract, register_sync_event_contract


def _noop_serialize(domain_event):
    return "attempt", domain_event.aggregate_id, {}


def _noop_validate(payload):
    pass


def test_unregistered_domain_event_is_local_only():
    assert get_sync_event_contract("internal.example.unregistered", 1) is None


def test_registry_is_keyed_by_exact_type_and_version():
    contract = SyncEventContract(
        event_type="example.registered",
        schema_version=1,
        serialize_domain_event=_noop_serialize,
        validate_payload=_noop_validate,
    )
    register_sync_event_contract(contract)

    assert get_sync_event_contract("example.registered", 1) is contract
    assert get_sync_event_contract("example.registered", 2) is None
    assert get_sync_event_contract("example.unrelated", 1) is None


def test_registry_rejects_duplicate_registration_of_the_same_key():
    contract = SyncEventContract(
        event_type="example.dup",
        schema_version=1,
        serialize_domain_event=_noop_serialize,
        validate_payload=_noop_validate,
    )
    register_sync_event_contract(contract)

    with pytest.raises(ValueError, match="already registered"):
        register_sync_event_contract(contract)
