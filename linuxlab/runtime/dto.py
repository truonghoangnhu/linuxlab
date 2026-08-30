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
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RuntimePackageContext:
    """An immutable request/use-case scoped locator and identity claim
    (T13-D08, T13-D09). `package_pk` is a local implementation detail
    only -- never public/cross-node identity -- used solely to reload and
    rebind against the persisted InstalledPackage on every lookup."""

    package_pk: int
    package_type: str
    source_public_id: UUID
    package_version: int
    payload_sha256: str


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
