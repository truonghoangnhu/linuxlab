"""Task 14 fail-closed syncable-event registry (T14-D20, T14-D21, T14-D22).

Absence from this registry means LOCAL-ONLY on Node / unsupported on
Central -- there is no default-syncable DomainEvent type, and Central
never guesses/best-effort-parses an unregistered (event_type,
schema_version) pair. This module is intentionally empty of any specific
contract; linuxlab.sync.enqueue registers the one real Task 14 vertical
slice (`activity.completed`) at import time.
"""

_REGISTRY = {}


def register_sync_event_contract(contract):
    """Register one SyncEventContract, keyed by its exact
    (event_type, schema_version). Rejects re-registering the same key --
    a contract is fixed for its schema_version; a changed contract must
    use a new schema_version, never overwrite the registered one."""
    key = (contract.event_type, contract.schema_version)
    if key in _REGISTRY:
        raise ValueError(f"a contract for {key!r} is already registered")
    _REGISTRY[key] = contract


def get_sync_event_contract(event_type, schema_version):
    """Return the registered SyncEventContract for this exact
    (event_type, schema_version), or None if it is not registered (i.e.
    LOCAL-ONLY / unsupported). Never falls back to a different version."""
    return _REGISTRY.get((event_type, schema_version))
