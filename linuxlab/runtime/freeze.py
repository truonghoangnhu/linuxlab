"""Deep-immutable JSON conversion for the Task 13 runtime boundary (T13-D05).

`@dataclass(frozen=True)` only protects field assignment on the DTO itself;
it does nothing about a nested dict/list still being the same mutable
object a caller (or a future bug) could reach and mutate in place. Every
DTO JSON-bearing field is therefore passed through freeze_json_value()
before it crosses the runtime boundary, producing a detached, recursively
immutable value that shares no mutable container with its source.
"""

from types import MappingProxyType

_SCALAR_TYPES = (str, int, float, bool, type(None))


def freeze_json_value(value):
    """Recursively convert a JSON-shaped value into an immutable
    equivalent: dict -> MappingProxyType, list/tuple -> tuple, JSON
    scalars unchanged. Raises TypeError for anything else, rather than
    silently passing a mutable/unrecognized runtime value through."""
    if isinstance(value, dict):
        return MappingProxyType(
            {key: freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json_value(item) for item in value)
    if isinstance(value, _SCALAR_TYPES):
        return value
    raise TypeError(f"freeze_json_value() does not support {type(value).__name__!r} values")
