"""Task 13 runtime DTO contracts (T13-D04, T13-D06).

Runtime services return these frozen dataclasses, never Django model
instances and never plain mutable dicts as their public contract. Every
JSON-bearing field is expected to already be the output of
freeze_json_value() by the time it reaches one of these constructors --
the DTOs themselves do not call it, since context.py/content.py/grading.py
own exactly when/what to freeze.

RuntimeQuestion (learner-safe) and RuntimeQuestionGrading (server-only)
are deliberately separate dataclasses: RuntimeQuestion has no
answer_key/scoring fields at all, so there is no field to accidentally
leave populated on a learner-facing DTO (T13-D06).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class RuntimePackageContext:
    """An immutable request/use-case scoped locator and identity claim
    (T13-D08, T13-D09). `package_pk` is a local implementation detail
    only -- never public/cross-node identity -- used solely to reload and
    rebind against the persisted InstalledPackage on every lookup.

    `_provenance_tag` is a same-process HMAC seal (Task 13 corrective)
    proving this context was minted by open_runtime_context() while its
    package was ACTIVE -- identity-field equality alone does not prove
    that; a manually constructed context matching an ARCHIVED/STAGED
    package's exact persisted identity is otherwise indistinguishable
    from a legitimate one. It is excluded from repr (never rendered/
    logged) and from equality/hash (context identity is still just the
    five claim fields). There is no default: a context missing this
    field entirely is exactly as invalid as one with a wrong value.
    """

    package_pk: int
    package_type: str
    source_public_id: UUID
    package_version: int
    payload_sha256: str
    _provenance_tag: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class RuntimePublication:
    kind: str
    source_public_id: UUID
    version: int
    content_hash: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class RuntimeActivity:
    activity_public_id: UUID
    version: int
    type_key: str
    config: Mapping[str, object]


@dataclass(frozen=True)
class RuntimeQuestion:
    question_public_id: UUID
    version: int
    type_key: str
    content: Mapping[str, object]


@dataclass(frozen=True)
class RuntimeQuestionGrading:
    question_public_id: UUID
    version: int
    answer_key: Mapping[str, object]
    scoring: Mapping[str, object]
