"""Signed immutable `.llpkg` packages (ADR-010, ADR-014, Task 11).

`.llpkg` is a TRUSTED CENTRAL -> NODE DEPLOYMENT ARTIFACT, not a
learner-download artifact: Ed25519 gives it authenticity/integrity, never
confidentiality, so it may legitimately carry server-only assessment
grading material (answer_key/scoring) an offline Node needs, as long as
that material stays in an explicitly separate `server` section that no
learner-facing/browser path ever reads (Task 11 correction).

Task 09 already produces the immutable artifact this module packages:

    mutable Wagtail authoring -> coherent validated PublicationSnapshot
        -> immutable payload + content_hash

build_package() accepts a PublicationSnapshot only as a LOCATOR/CLAIM: it
reloads the authoritative persisted row by primary key, rejects an unsaved
instance outright, rejects any in-memory field that disagrees with the
persisted row (so a mutated caller object can never drive what gets
signed), and independently recomputes the row's own content_hash before
trusting it (Task 11 correction: PublicationSnapshot is otherwise
trustworthy only through its own supported production APIs, which
build_package() must not bypass by trusting arbitrary attributes).

From that authoritative row, build_package() resolves the EXACT immutable
ActivityVersion/QuestionVersion closure the publication depends on -- never
"latest" -- and assembles a deterministic, self-contained package payload
envelope with FOUR sections: `publication` (the Task 09 snapshot, embedded
verbatim), `activities`, `questions` (learner-safe), and `server.questions`
(grading data). This is what lets a clean offline Node stage everything a
publication needs without ever touching Central's database or the
Internet again (Task 12's concern; Task 11 only provides the artifact).

The package is a signed v1 `.llpkg` archive (a ZIP container) with exactly
three members:

    manifest.json   -- canonical PackageManifest fields
    payload.json    -- the canonical package payload envelope described
                       above (NOT merely PublicationSnapshot.payload)
    signature.bin   -- a raw 64-byte Ed25519 signature over
                       manifest.json bytes + payload.json bytes

verify_package() is the sole trust boundary: it takes the trusted activity
registry (internal DI, never attacker-controlled package data) and an
EXTERNALLY supplied trusted public key -- never a key embedded in the
package -- and either returns a validated VerifiedPackage or raises
PackageVerificationError. It fully validates archive structure, the
manifest, the payload envelope's shape (including cross-checking manifest
identity against the embedded publication identity, and independently
recomputing the embedded Task 09 content_hash), the EXACT dependency
closure (every publication activity reference has a matching activity
artifact and no orphans, every activity's exact question dependencies have
matching learner/server artifacts and no orphans -- using the SAME pure
_collect_activity_refs() walker build_package() uses, so the two can never
silently diverge), and the signature, before returning anything. It never
touches the database (everything it needs is the package bytes, the
trusted registry, and the trusted key) and is otherwise side-effect free:
no extraction, no activation (Task 12 owns package lifecycle).

verify_package() returns bytes, not a parsed mutable dict: VerifiedPackage
carries only the frozen PackageManifest plus the exact immutable
`package_bytes`/`payload_bytes` that were cryptographically verified --
there is no supported way to mutate "the verified payload" through
VerifiedPackage, because there is no authoritative parsed payload exposed
by it at all. A caller may independently parse `payload_bytes` into its own
dict, but that parse is the caller's own unauthoritative interpretation,
not the trust-boundary object. Task 12's future staging must accept a
VerifiedPackage (or its payload_bytes) and parse+stage in the same trusted
operation -- never accept a caller-supplied replacement payload dict as
equivalent to a verified one.
"""

import json
import lzma
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO

from cryptography.exceptions import InvalidSignature
from django.db import transaction
from django.utils import timezone

from linuxlab.publication.signing import SIGNATURE_ALGORITHM, sign_bytes, verify_signature
from linuxlab.publication.snapshots import canonical_json_bytes, compute_content_hash

PACKAGE_SCHEMA_VERSION = 1
MINIMUM_LINUXLAB_VERSION = "0.1.0"

MANIFEST_MEMBER = "manifest.json"
PAYLOAD_MEMBER = "payload.json"
SIGNATURE_MEMBER = "signature.bin"
_REQUIRED_MEMBERS = frozenset({MANIFEST_MEMBER, PAYLOAD_MEMBER, SIGNATURE_MEMBER})

# ZIP general-purpose bit flag bits (ZIP APPNOTE section 4.4.4): bit 0 is
# traditional PKWARE encryption, bit 6 is the (rarer) strong-encryption
# flag. LinuxLab .llpkg packages are Central-built and never encrypted --
# checked explicitly against member metadata, rather than relying on
# zipfile's own password-prompt RuntimeError, so an encrypted member is a
# deliberate domain rejection, not an incidental side effect.
_ZIP_ENCRYPTED_FLAG_BITS = (1 << 0) | (1 << 6)

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "package_uuid",
        "package_type",
        "package_version",
        "created_at",
        "payload_sha256",
        "minimum_linuxlab_version",
        "signature_algorithm",
    }
)

_PAYLOAD_TOP_LEVEL_FIELDS = frozenset({"publication", "activities", "questions", "server"})
_PUBLICATION_FIELDS = frozenset({"kind", "source_public_id", "version", "content_hash", "payload"})
_ACTIVITY_FIELDS = frozenset({"activity_public_id", "version", "type_key", "config"})
_QUESTION_FIELDS = frozenset({"question_public_id", "version", "type_key", "content"})
_SERVER_FIELDS = frozenset({"questions"})
_SERVER_QUESTION_FIELDS = frozenset({"question_public_id", "version", "answer_key", "scoring"})

_SUPPORTED_PUBLICATION_KINDS = frozenset({"learning_path"})


class PackageBuildError(Exception):
    pass


class PackageVerificationError(Exception):
    pass


@dataclass(frozen=True)
class PackageManifest:
    schema_version: int
    package_uuid: uuid.UUID
    package_type: str
    package_version: int
    created_at: datetime
    payload_sha256: str
    minimum_linuxlab_version: str
    signature_algorithm: str


@dataclass(frozen=True)
class VerifiedPackage:
    """The full, fully-validated result of verify_package(): the manifest
    (scalar/frozen fields only) plus the EXACT immutable bytes that were
    cryptographically verified. This is the trust boundary Task 12 must
    stage from -- bytes, not a parsed mutable dict, precisely because a
    parsed dict/list graph is mutable after the fact even inside a frozen
    dataclass (Task 11 final correction, Critical): reassigning
    `verified.manifest` fails (frozen), but `verified.payload["x"] = y`
    would NOT have failed on the prior payload:dict design, letting
    unsigned data cross the verify -> stage boundary silently.

    `package_bytes` is the exact archive bytes verify_package() was given
    (defensively copied via bytes() so a caller's mutable bytearray/
    memoryview input can never later change what this object reports).
    `payload_bytes` is the exact stored payload.json member bytes: the
    same bytes that were SHA-256 checked against the manifest, included in
    the signed message, JSON-parsed, schema-validated, and
    dependency-closure-validated. Nothing here is re-serialized/
    re-canonicalized before being returned.

    A caller may independently `json.loads(verified.payload_bytes)` to get
    a working dict -- that parse is the caller's own unauthoritative
    interpretation, not a second copy of the trust boundary.
    """

    manifest: PackageManifest
    package_bytes: bytes
    payload_bytes: bytes


def _manifest_dict(kind, source_public_id, version, created_at, payload_sha256):
    return {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_uuid": str(source_public_id),
        "package_type": kind,
        "package_version": version,
        "created_at": created_at.isoformat(),
        "payload_sha256": payload_sha256,
        "minimum_linuxlab_version": MINIMUM_LINUXLAB_VERSION,
        "signature_algorithm": SIGNATURE_ALGORITHM,
    }


# --- build: authoritative source -----------------------------------------


def _load_authoritative_snapshot(candidate):
    """Reload the authoritative persisted PublicationSnapshot row by
    primary key, and require it to agree with the caller-supplied
    instance's own semantic fields (Task 11 correction, Critical):
    build_package() must not sign whatever attributes a mutable/unsaved/
    in-memory-modified PublicationSnapshot-like object happens to carry.
    The caller's object is treated as a LOCATOR/CLAIM only.
    """
    from linuxlab.publication.models import PublicationSnapshot

    if not isinstance(candidate, PublicationSnapshot):
        raise PackageBuildError("build_package() requires a PublicationSnapshot instance")

    if candidate._state.adding or candidate.pk is None:
        raise PackageBuildError(
            "build_package() requires an already-persisted PublicationSnapshot; "
            "an unsaved instance was never accepted Task 09 publication history"
        )

    try:
        authoritative = PublicationSnapshot.objects.get(pk=candidate.pk)
    except PublicationSnapshot.DoesNotExist as exc:
        raise PackageBuildError("PublicationSnapshot no longer exists") from exc

    for field in ("kind", "source_public_id", "version", "payload", "content_hash"):
        if getattr(candidate, field) != getattr(authoritative, field):
            raise PackageBuildError(
                f"PublicationSnapshot.{field} does not match its persisted row; "
                "refusing to package possibly-mutated in-memory state"
            )

    # Defense against database corruption/raw mutation/programmer misuse
    # bypassing Task 09's own supported write path (Task 11 correction):
    # re-derive the hash independently rather than trusting the stored
    # content_hash column at face value.
    recomputed_hash = compute_content_hash(authoritative.payload)
    if recomputed_hash != authoritative.content_hash:
        raise PackageBuildError(
            "PublicationSnapshot.content_hash does not match the recomputed "
            "canonical hash of its own payload; refusing to package"
        )

    if authoritative.kind not in _SUPPORTED_PUBLICATION_KINDS:
        raise PackageBuildError(f"unsupported publication kind '{authoritative.kind}'")

    return authoritative


# --- shared: pure, DB-free reference walking ------------------------------
# Used by BOTH build_package() and verify_package() so the two can never
# silently parse the Task 09 publication payload differently (Task 11
# final correction, section 14: a shared reference walker, not two
# independent implementations that could drift apart).


def _collect_activity_refs(publication_payload):
    refs = set()
    for module in publication_payload["learning_path"]["modules"]:
        for lesson in module["lessons"]:
            for block in lesson["blocks"]:
                if block["type"] == "activity":
                    value = block["value"]
                    refs.add(
                        (uuid.UUID(str(value["activity_public_id"])), value["activity_version"])
                    )
    return refs


def _resolve_provider(type_key, activity_registry, error_cls):
    """Fail closed (Task 11 final correction): a package whose activity
    references a type_key with no registered provider has unknown runtime
    behavior and must not be considered buildable or deployable.
    """
    try:
        return activity_registry.get(type_key)
    except LookupError as exc:
        raise error_cls(f"no provider registered for type_key '{type_key}'") from exc


def _collect_provider_dependencies(provider, config, error_cls):
    """Fail closed (Task 11 final correction): collect_dependencies() is a
    REQUIRED part of the ActivityProvider contract. Silence -- a provider
    missing the method entirely -- is never interpreted as "no
    dependencies"; only an explicit ActivityDependencies.empty() return
    value means that. This is what stops a future dependency-bearing
    provider that forgets to implement the method from silently producing
    an incomplete package.
    """
    collect_dependencies = getattr(provider, "collect_dependencies", None)
    if collect_dependencies is None:
        raise error_cls(
            f"provider for type_key '{provider.type_key}' does not implement "
            "collect_dependencies(); every registered ActivityProvider must "
            "declare its exact dependencies explicitly, using "
            "ActivityDependencies.empty() if it has none"
        )
    try:
        return collect_dependencies(config)
    except (ValueError, TypeError) as exc:
        raise error_cls(f"provider dependency discovery failed: {exc}") from exc


# --- build: exact immutable dependency closure ----------------------------


def _resolve_activities(activity_refs, activity_registry):
    from linuxlab.activities.models import ActivityVersion
    from linuxlab.activities.services import get_activity_version

    activities = []
    question_refs = set()

    for activity_public_id, version in sorted(activity_refs, key=lambda r: (str(r[0]), r[1])):
        try:
            activity_version = get_activity_version(activity_public_id, version)
        except ActivityVersion.DoesNotExist as exc:
            raise PackageBuildError(
                f"no published ActivityVersion for {activity_public_id} v{version}"
            ) from exc

        type_key = activity_version.definition.type_key
        activities.append(
            {
                "activity_public_id": str(activity_public_id),
                "version": version,
                "type_key": type_key,
                "config": activity_version.config,
            }
        )

        provider = _resolve_provider(type_key, activity_registry, PackageBuildError)
        dependencies = _collect_provider_dependencies(
            provider, activity_version.config, PackageBuildError
        )
        question_refs.update(dependencies.question_versions)

    activities.sort(key=lambda a: (a["activity_public_id"], a["version"]))
    return activities, question_refs


def _resolve_questions(question_refs):
    from linuxlab.assessment.models import QuestionVersion
    from linuxlab.assessment.services import get_question_version

    learner_questions = []
    server_questions = []

    for question_public_id, version in sorted(question_refs, key=lambda r: (str(r[0]), r[1])):
        try:
            question_version = get_question_version(question_public_id, version)
        except QuestionVersion.DoesNotExist as exc:
            raise PackageBuildError(
                f"no published QuestionVersion for {question_public_id} v{version}"
            ) from exc

        learner_questions.append(
            {
                "question_public_id": str(question_public_id),
                "version": version,
                "type_key": question_version.type_key,
                "content": question_version.content,
            }
        )
        server_questions.append(
            {
                "question_public_id": str(question_public_id),
                "version": version,
                "answer_key": question_version.answer_key,
                "scoring": question_version.scoring,
            }
        )

    return learner_questions, server_questions


def _build_package_payload(snapshot, activity_registry):
    activity_refs = _collect_activity_refs(snapshot.payload)
    activities, question_refs = _resolve_activities(activity_refs, activity_registry)
    learner_questions, server_questions = _resolve_questions(question_refs)

    return {
        "publication": {
            "kind": snapshot.kind,
            "source_public_id": str(snapshot.source_public_id),
            "version": snapshot.version,
            "content_hash": snapshot.content_hash,
            "payload": snapshot.payload,
        },
        "activities": activities,
        "questions": learner_questions,
        "server": {
            "questions": server_questions,
        },
    }


def build_package(snapshot, activity_registry, private_key):
    """Build a signed v1 .llpkg archive from an accepted, persisted
    PublicationSnapshot and its exact immutable ActivityVersion/
    QuestionVersion dependency closure, and return the raw archive bytes.

    `snapshot` is only a locator/claim -- see _load_authoritative_snapshot().
    `activity_registry` resolves each referenced ActivityVersion's provider
    (the same registry publish_learning_path() itself takes), used only to
    discover dependencies, never to mutate anything.

    Package identity (package_uuid/package_type/package_version) mirrors
    the snapshot's own stable identity (source_public_id/kind/version) --
    never PublicationSnapshot's local primary key.

    `private_key` is an Ed25519PrivateKey supplied explicitly by the
    caller; it is never read from settings, the database, or a file here,
    and this function never logs or otherwise persists it.
    """
    # A plain transaction groups the dependency-closure reads together so a
    # concurrent deletion can't be observed mid-build in a torn way; every
    # row involved is immutable once published, so nothing stronger (no
    # REPEATABLE READ, no advisory mutex -- Task 09's publication
    # concurrency machinery has no place here) is needed.
    with transaction.atomic():
        authoritative = _load_authoritative_snapshot(snapshot)
        package_payload = _build_package_payload(authoritative, activity_registry)

    payload_bytes = canonical_json_bytes(package_payload)
    payload_sha256 = compute_content_hash(package_payload)

    manifest_dict = _manifest_dict(
        authoritative.kind,
        authoritative.source_public_id,
        authoritative.version,
        timezone.now(),
        payload_sha256,
    )
    manifest_bytes = canonical_json_bytes(manifest_dict)

    # The signature covers the raw bytes that will actually be stored, not
    # a value recomputed at verify time -- so ANY byte-level change to
    # either stored member, even a re-serialization that would parse to an
    # equivalent value, invalidates the signature.
    signature = sign_bytes(manifest_bytes + payload_bytes, private_key)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_MEMBER, manifest_bytes)
        archive.writestr(PAYLOAD_MEMBER, payload_bytes)
        archive.writestr(SIGNATURE_MEMBER, signature)

    return buffer.getvalue()


# --- verify: archive structure --------------------------------------------


def _read_exact_members(archive):
    # zipfile.namelist() includes duplicate names verbatim (a ZIP
    # container permits them); archive.read(name) silently resolves to
    # the LAST matching entry, which would let a duplicate manifest.json
    # or payload.json member masquerade as "the" member read elsewhere.
    # Rejecting any duplicate before ever calling .read() closes that
    # ambiguity outright rather than relying on zipfile's read order.
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise PackageVerificationError("package contains duplicate archive members")

    name_set = set(names)
    if name_set != _REQUIRED_MEMBERS:
        missing = sorted(_REQUIRED_MEMBERS - name_set)
        extra = sorted(name_set - _REQUIRED_MEMBERS)
        raise PackageVerificationError(
            f"package does not contain exactly the required v1 members "
            f"(missing={missing}, extra={extra})"
        )

    # Reject encryption deliberately, from member metadata, before ever
    # attempting to read a byte of member content -- not by letting
    # zipfile's own password-prompt RuntimeError leak through
    # incidentally (Corrective A final review FA-I1).
    for name in _REQUIRED_MEMBERS:
        if archive.getinfo(name).flag_bits & _ZIP_ENCRYPTED_FLAG_BITS:
            raise PackageVerificationError(f"package member {name!r} is encrypted")

    try:
        return {name: archive.read(name) for name in _REQUIRED_MEMBERS}
    except NotImplementedError as exc:
        # archive.read() dispatches to a decompressor purely by the
        # member's declared compression-method NUMBER (zipfile's
        # _get_decompressor()); a well-formed container can declare a
        # method zipfile does not implement, which is untrusted-input
        # rejection, not a LinuxLab programming defect -- this package
        # was never producible by build_package(), which always uses
        # ZIP_DEFLATED.
        raise PackageVerificationError(
            "package member uses an unsupported archive compression method"
        ) from exc
    except RuntimeError as exc:
        # Defense in depth alongside the explicit flag_bits check above:
        # covers any zipfile-internal encryption rejection (e.g. the
        # strong-encryption combination) the metadata pre-check did not
        # anticipate. Never reachable for the two flag bits checked
        # above, since those are rejected before archive.read() runs.
        raise PackageVerificationError("package member is encrypted") from exc
    except (zipfile.BadZipFile, zlib.error, OSError, lzma.LZMAError) as exc:
        # archive.read() decompresses each member entirely in memory
        # (package_bytes is a BytesIO, never a real file), so an OSError
        # here can only be a decompressor's own data-validity check --
        # e.g. bz2's `OSError: Invalid data stream` on a corrupted BZIP2
        # member -- never a genuine filesystem error. A structurally
        # well-formed ZIP container can similarly hold an invalid
        # DEFLATE byte stream for one member, which raises zlib.error
        # directly from the decompressor rather than zipfile.BadZipFile.
        # A ZIP_LZMA member with valid framing but a malformed embedded
        # LZMA1 filter-properties block (zipfile.LZMADecompressor calls
        # lzma._decode_filter_properties() on those bytes before ever
        # touching the compressed stream) raises lzma.LZMAError directly
        # -- a plain Exception subclass, not an OSError, so it needs its
        # own explicit entry here (residual FA-I1, round 2). All four are
        # the same trust boundary here: malformed compressed package
        # content, normalized to one domain rejection.
        raise PackageVerificationError("corrupt archive member") from exc


# --- verify: manifest -------------------------------------------------


def _parse_manifest(manifest_bytes):
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PackageVerificationError("manifest.json is not valid JSON") from exc

    if not isinstance(manifest, dict):
        raise PackageVerificationError("manifest.json must be a JSON object")

    actual_fields = set(manifest.keys())
    if actual_fields != _MANIFEST_FIELDS:
        missing = sorted(_MANIFEST_FIELDS - actual_fields)
        extra = sorted(actual_fields - _MANIFEST_FIELDS)
        raise PackageVerificationError(
            f"manifest.json does not have exactly the required v1 fields "
            f"(missing={missing}, extra={extra})"
        )

    # type(x) is int, not equality alone: True == 1 in Python, so a plain
    # `!= PACKAGE_SCHEMA_VERSION` check would silently accept
    # schema_version=true as version 1 (Task 11 corrective bug fix).
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != PACKAGE_SCHEMA_VERSION
    ):
        raise PackageVerificationError(
            f"unsupported package schema version {manifest['schema_version']!r}"
        )
    if manifest["signature_algorithm"] != SIGNATURE_ALGORITHM:
        raise PackageVerificationError(
            f"unsupported signature algorithm {manifest['signature_algorithm']!r}"
        )

    try:
        package_uuid = uuid.UUID(str(manifest["package_uuid"]))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PackageVerificationError("manifest.json package_uuid is not a valid UUID") from exc

    if type(manifest["package_version"]) is not int or manifest["package_version"] < 1:
        raise PackageVerificationError("manifest.json package_version must be an integer >= 1")

    if not isinstance(manifest["package_type"], str) or not manifest["package_type"]:
        raise PackageVerificationError("manifest.json package_type must be a non-empty string")

    if (
        not isinstance(manifest["minimum_linuxlab_version"], str)
        or not manifest["minimum_linuxlab_version"]
    ):
        raise PackageVerificationError(
            "manifest.json minimum_linuxlab_version must be a non-empty string"
        )

    payload_sha256 = manifest["payload_sha256"]
    if not isinstance(payload_sha256, str) or len(payload_sha256) != 64:
        raise PackageVerificationError(
            "manifest.json payload_sha256 must be a 64-character hex digest"
        )
    try:
        int(payload_sha256, 16)
    except ValueError as exc:
        raise PackageVerificationError(
            "manifest.json payload_sha256 must be a 64-character hex digest"
        ) from exc

    try:
        created_at = datetime.fromisoformat(manifest["created_at"])
    except (ValueError, TypeError) as exc:
        raise PackageVerificationError(
            "manifest.json created_at is not a valid ISO-8601 timestamp"
        ) from exc

    return {
        "schema_version": manifest["schema_version"],
        "package_uuid": package_uuid,
        "package_type": manifest["package_type"],
        "package_version": manifest["package_version"],
        "created_at": created_at,
        "payload_sha256": payload_sha256,
        "minimum_linuxlab_version": manifest["minimum_linuxlab_version"],
        "signature_algorithm": manifest["signature_algorithm"],
    }


# --- verify: payload envelope -------------------------------------------


def _require_uuid(value, what):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PackageVerificationError(f"{what} is not a valid UUID") from exc


def _require_exact_version(value, what):
    if type(value) is not int or value < 1:
        raise PackageVerificationError(f"{what} must be an integer >= 1")


def _require_hex_digest(value, what):
    if not isinstance(value, str) or len(value) != 64:
        raise PackageVerificationError(f"{what} must be a 64-character hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise PackageVerificationError(f"{what} must be a 64-character hex digest") from exc


def _require_nonempty_str(value, what):
    if not isinstance(value, str) or not value:
        raise PackageVerificationError(f"{what} must be a non-empty string")


def _require_object(value, what):
    if not isinstance(value, dict):
        raise PackageVerificationError(f"{what} must be a JSON object")


def _require_exact_fields(entry, required_fields, what):
    if not isinstance(entry, dict):
        raise PackageVerificationError(f"{what} must be a JSON object")
    actual = set(entry.keys())
    if actual != required_fields:
        missing = sorted(required_fields - actual)
        extra = sorted(actual - required_fields)
        raise PackageVerificationError(
            f"{what} does not have exactly the required fields "
            f"(missing={missing}, extra={extra})"
        )


def _validate_publication_section(publication):
    _require_exact_fields(publication, _PUBLICATION_FIELDS, "payload.publication")

    _require_nonempty_str(publication["kind"], "payload.publication.kind")
    _require_uuid(publication["source_public_id"], "payload.publication.source_public_id")
    _require_exact_version(publication["version"], "payload.publication.version")
    _require_hex_digest(publication["content_hash"], "payload.publication.content_hash")
    _require_object(publication["payload"], "payload.publication.payload")

    recomputed = compute_content_hash(publication["payload"])
    if recomputed != publication["content_hash"]:
        raise PackageVerificationError(
            "payload.publication.content_hash does not match the recomputed "
            "canonical hash of payload.publication.payload"
        )


def _validate_activities_section(activities):
    if not isinstance(activities, list):
        raise PackageVerificationError("payload.activities must be a list")

    seen = set()
    for entry in activities:
        _require_exact_fields(entry, _ACTIVITY_FIELDS, "payload.activities entry")
        activity_public_id = _require_uuid(entry["activity_public_id"], "activity_public_id")
        _require_exact_version(entry["version"], "activity version")
        _require_nonempty_str(entry["type_key"], "activity type_key")
        _require_object(entry["config"], "activity config")

        key = (activity_public_id, entry["version"])
        if key in seen:
            raise PackageVerificationError(
                f"duplicate activity artifact {activity_public_id} v{entry['version']}"
            )
        seen.add(key)


def _validate_questions_section(questions):
    if not isinstance(questions, list):
        raise PackageVerificationError("payload.questions must be a list")

    seen = set()
    for entry in questions:
        _require_exact_fields(entry, _QUESTION_FIELDS, "payload.questions entry")
        question_public_id = _require_uuid(entry["question_public_id"], "question_public_id")
        _require_exact_version(entry["version"], "question version")
        _require_nonempty_str(entry["type_key"], "question type_key")
        _require_object(entry["content"], "question content")

        key = (question_public_id, entry["version"])
        if key in seen:
            raise PackageVerificationError(
                f"duplicate question artifact {question_public_id} v{entry['version']}"
            )
        seen.add(key)

    return seen


def _validate_server_section(server, learner_question_keys):
    _require_exact_fields(server, _SERVER_FIELDS, "payload.server")

    server_questions = server["questions"]
    if not isinstance(server_questions, list):
        raise PackageVerificationError("payload.server.questions must be a list")

    seen = set()
    for entry in server_questions:
        _require_exact_fields(entry, _SERVER_QUESTION_FIELDS, "payload.server.questions entry")
        question_public_id = _require_uuid(
            entry["question_public_id"], "server question_public_id"
        )
        _require_exact_version(entry["version"], "server question version")
        _require_object(entry["answer_key"], "server question answer_key")
        _require_object(entry["scoring"], "server question scoring")

        key = (question_public_id, entry["version"])
        if key in seen:
            raise PackageVerificationError(
                f"duplicate server question artifact {question_public_id} v{entry['version']}"
            )
        seen.add(key)

    # Every learner-safe question that needs grading must have exactly one
    # server counterpart, and no server entry may exist without a learner
    # counterpart (Task 11 correction: no orphan grading data, no
    # ungraded packaged question).
    if seen != learner_question_keys:
        missing_server = sorted(
            learner_question_keys - seen, key=lambda k: (str(k[0]), k[1])
        )
        orphan_server = sorted(seen - learner_question_keys, key=lambda k: (str(k[0]), k[1]))
        raise PackageVerificationError(
            "payload.server.questions does not exactly match payload.questions "
            f"(missing_server={missing_server}, orphan_server={orphan_server})"
        )


def _parse_payload(payload_bytes):
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PackageVerificationError("payload.json is not valid JSON") from exc

    _require_exact_fields(payload, _PAYLOAD_TOP_LEVEL_FIELDS, "payload.json")

    _validate_publication_section(payload["publication"])
    learner_question_keys = _validate_questions_section(payload["questions"])
    _validate_activities_section(payload["activities"])
    _validate_server_section(payload["server"], learner_question_keys)

    return payload


# --- verify: exact dependency closure --------------------------------------
# Proves the package is not merely well-formed but SELF-CONTAINED: every
# publication activity reference has a matching activity artifact (no
# missing, no orphan), and every activity's exact question dependencies
# have matching learner artifacts (already known, transitively, to have
# matching server artifacts too, via _validate_server_section above). A
# correctly-signed package that omits or adds an artifact must fail here
# even though its signature is genuinely valid -- cryptographic
# authenticity alone does not prove structural completeness.


def _verify_activity_closure(payload):
    expected_activity_refs = _collect_activity_refs(payload["publication"]["payload"])
    actual_activity_refs = {
        (uuid.UUID(entry["activity_public_id"]), entry["version"])
        for entry in payload["activities"]
    }

    if expected_activity_refs != actual_activity_refs:
        missing = sorted(
            expected_activity_refs - actual_activity_refs, key=lambda k: (str(k[0]), k[1])
        )
        orphan = sorted(
            actual_activity_refs - expected_activity_refs, key=lambda k: (str(k[0]), k[1])
        )
        raise PackageVerificationError(
            "payload.activities does not exactly match the publication's activity "
            f"references (missing={missing}, orphan={orphan})"
        )

    return payload["activities"]


def _verify_question_closure(activities, questions, activity_registry):
    expected_question_refs = set()
    for entry in activities:
        provider = _resolve_provider(entry["type_key"], activity_registry, PackageVerificationError)
        dependencies = _collect_provider_dependencies(
            provider, entry["config"], PackageVerificationError
        )
        expected_question_refs.update(dependencies.question_versions)

    actual_question_refs = {
        (uuid.UUID(entry["question_public_id"]), entry["version"]) for entry in questions
    }

    if expected_question_refs != actual_question_refs:
        missing = sorted(
            expected_question_refs - actual_question_refs, key=lambda k: (str(k[0]), k[1])
        )
        orphan = sorted(
            actual_question_refs - expected_question_refs, key=lambda k: (str(k[0]), k[1])
        )
        raise PackageVerificationError(
            "payload.questions does not exactly match the activity dependency "
            f"closure (missing={missing}, orphan={orphan})"
        )


def _verify_dependency_closure(payload, activity_registry):
    activities = _verify_activity_closure(payload)
    _verify_question_closure(activities, payload["questions"], activity_registry)
    # payload.server.questions == payload.questions is already enforced by
    # _validate_server_section() inside _parse_payload(), so the closure
    # activities -> questions -> server.questions is now fully proven.


def verify_package(package_bytes, activity_registry, trusted_public_key):
    """Verify a .llpkg archive's structure, integrity, payload schema,
    EXACT dependency closure, and Ed25519 signature, and return its
    validated VerifiedPackage -- or raise PackageVerificationError.

    `activity_registry` is a trusted internal dependency-injection
    boundary (never attacker-controlled package data) used to resolve each
    packaged activity's provider for dependency-closure verification.

    `trusted_public_key` is an Ed25519PublicKey supplied EXTERNALLY by the
    caller (e.g. Node configuration) -- this function never reads a key
    from the package itself, because a package proving self-consistency
    with its own embedded key would prove nothing about the publisher's
    authenticity (ADR-010).

    Never touches the database. Otherwise side-effect free: no extraction
    to disk, no activation.
    """
    # Defensive immutable copy: a caller-supplied bytearray/memoryview must
    # not be able to change what this function verifies OR what the
    # returned VerifiedPackage.package_bytes later reports, if the caller
    # mutates their own buffer after this call returns.
    package_bytes = bytes(package_bytes)

    try:
        archive = zipfile.ZipFile(BytesIO(package_bytes))
    except zipfile.BadZipFile as exc:
        raise PackageVerificationError("not a valid .llpkg archive") from exc

    with archive:
        members = _read_exact_members(archive)

    manifest_bytes = members[MANIFEST_MEMBER]
    payload_bytes = members[PAYLOAD_MEMBER]
    signature = members[SIGNATURE_MEMBER]

    manifest_fields = _parse_manifest(manifest_bytes)

    actual_payload_sha256 = sha256(payload_bytes).hexdigest()
    if actual_payload_sha256 != manifest_fields["payload_sha256"]:
        raise PackageVerificationError(
            "payload.json digest does not match manifest.json payload_sha256"
        )

    try:
        verify_signature(manifest_bytes + payload_bytes, signature, trusted_public_key)
    except InvalidSignature as exc:
        raise PackageVerificationError("package signature is invalid") from exc

    payload = _parse_payload(payload_bytes)

    publication = payload["publication"]
    if manifest_fields["package_type"] != publication["kind"]:
        raise PackageVerificationError(
            "manifest package_type does not match payload.publication.kind"
        )
    if str(manifest_fields["package_uuid"]) != publication["source_public_id"]:
        raise PackageVerificationError(
            "manifest package_uuid does not match payload.publication.source_public_id"
        )
    if manifest_fields["package_version"] != publication["version"]:
        raise PackageVerificationError(
            "manifest package_version does not match payload.publication.version"
        )

    _verify_dependency_closure(payload, activity_registry)

    # `payload` (the parsed dict) has done its job -- structural and
    # closure validation -- and is discarded here. It is never part of the
    # returned trust boundary (Task 11 final correction).
    return VerifiedPackage(
        manifest=PackageManifest(**manifest_fields),
        package_bytes=package_bytes,
        payload_bytes=payload_bytes,
    )
