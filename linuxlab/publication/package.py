"""Signed immutable `.llpkg` packages (ADR-010, ADR-014, Task 11).

Task 09 already produces the immutable artifact this module packages:

    mutable Wagtail authoring -> coherent validated PublicationSnapshot
        -> immutable payload + content_hash

build_package() consumes a PublicationSnapshot ONLY -- never live Wagtail
pages, never mutable authoring state -- and produces a signed v1 `.llpkg`
archive (a ZIP container) with exactly three members:

    manifest.json   -- canonical PackageManifest fields
    payload.json    -- the snapshot's own canonical payload bytes, verbatim
    signature.bin   -- a raw 64-byte Ed25519 signature over
                       manifest.json bytes + payload.json bytes

verify_package() is the sole trust boundary: it takes an EXTERNALLY
supplied trusted public key -- never a key embedded in the package -- and
either returns a validated PackageManifest or raises
PackageVerificationError. It is side-effect free: no DB writes, no
extraction, no activation (Task 12 owns package lifecycle).
"""

import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO

from cryptography.exceptions import InvalidSignature
from django.utils import timezone

from linuxlab.publication.signing import SIGNATURE_ALGORITHM, sign_bytes, verify_signature
from linuxlab.publication.snapshots import canonical_json_bytes, compute_content_hash

PACKAGE_SCHEMA_VERSION = 1
MINIMUM_LINUXLAB_VERSION = "0.1.0"

MANIFEST_MEMBER = "manifest.json"
PAYLOAD_MEMBER = "payload.json"
SIGNATURE_MEMBER = "signature.bin"
_REQUIRED_MEMBERS = frozenset({MANIFEST_MEMBER, PAYLOAD_MEMBER, SIGNATURE_MEMBER})

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


def _manifest_dict(snapshot, created_at, payload_sha256):
    return {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_uuid": str(snapshot.source_public_id),
        "package_type": snapshot.kind,
        "package_version": snapshot.version,
        "created_at": created_at.isoformat(),
        "payload_sha256": payload_sha256,
        "minimum_linuxlab_version": MINIMUM_LINUXLAB_VERSION,
        "signature_algorithm": SIGNATURE_ALGORITHM,
    }


def build_package(snapshot, private_key):
    """Build a signed v1 .llpkg archive from an immutable PublicationSnapshot
    ONLY, and return the raw archive bytes.

    Package identity (package_uuid/package_type/package_version) mirrors
    the snapshot's own stable identity (source_public_id/kind/version) --
    never PublicationSnapshot's local primary key -- so republishing
    unchanged content into a new PublicationSnapshot version (Task 09:
    same content_hash, different version) correctly produces a package
    with the SAME payload digest but a DIFFERENT, non-deduplicated package
    identity.

    `private_key` is an Ed25519PrivateKey supplied explicitly by the
    caller; it is never read from settings, the database, or a file here,
    and this function never logs or otherwise persists it.
    """
    payload_bytes = canonical_json_bytes(snapshot.payload)
    # Identical inputs to Task 09's own hashing, so this always equals
    # snapshot.content_hash -- proven by test, not merely asserted here.
    payload_sha256 = compute_content_hash(snapshot.payload)

    manifest_dict = _manifest_dict(snapshot, timezone.now(), payload_sha256)
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

    return {name: archive.read(name) for name in _REQUIRED_MEMBERS}


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

    if manifest["schema_version"] != PACKAGE_SCHEMA_VERSION:
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

    # type(x) is int, not isinstance: isinstance(True, int) is True in
    # Python, which would let package_version=True silently match version 1
    # (matches the existing exact-version convention across this codebase).
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


def verify_package(package_bytes, trusted_public_key):
    """Verify a .llpkg archive's structure, integrity, and Ed25519
    signature, and return its validated PackageManifest -- or raise
    PackageVerificationError.

    `trusted_public_key` is an Ed25519PublicKey supplied EXTERNALLY by the
    caller (e.g. Node configuration) -- this function never reads a key
    from the package itself, because a package proving self-consistency
    with its own embedded key would prove nothing about the publisher's
    authenticity (ADR-010).

    Side-effect free: no DB writes, no extraction to disk, no activation.
    """
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

    return PackageManifest(**manifest_fields)
