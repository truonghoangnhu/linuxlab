import pytest

from linuxlab.activities.registry import ActivityRegistry


class FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        if "expected" not in config:
            raise ValueError("config must contain 'expected'")

    def evaluate(self, config, submission):
        return {"correct": submission == config["expected"]}


def test_provider_registers_and_can_be_looked_up():
    registry = ActivityRegistry()
    provider = FakeProvider()

    registry.register(provider)

    assert registry.get("fake") is provider


def test_duplicate_type_key_registration_is_rejected():
    registry = ActivityRegistry()
    registry.register(FakeProvider())

    with pytest.raises(ValueError):
        registry.register(FakeProvider())


def test_unknown_type_key_lookup_is_rejected():
    registry = ActivityRegistry()

    with pytest.raises(LookupError):
        registry.get("does-not-exist")


def test_registry_instances_are_isolated():
    first = ActivityRegistry()
    second = ActivityRegistry()

    first.register(FakeProvider())

    with pytest.raises(LookupError):
        second.get("fake")
