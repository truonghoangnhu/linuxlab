"""Task 11 final corrective: immutable VerifiedPackage boundary (exact
verified bytes, never a mutable parsed payload dict) + full dependency-
closure verification (publication -> activities -> questions -> server,
not merely per-artifact shape checks) + a provider dependency contract that
fails closed instead of silently defaulting to "no dependencies".

No mocks for the core dependency-closure path: real published Question/
ActivityVersion/PublicationSnapshot objects, a real ephemeral Ed25519
keypair, build and verify every package here."""

import dataclasses
import json
import struct
import uuid
import zipfile
from datetime import datetime
from hashlib import sha256
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.db import connection

from linuxlab.activities.contracts import ActivityDependencies
from linuxlab.activities.models import ActivityDefinition, ActivityVersion
from linuxlab.activities.registry import ActivityRegistry
from linuxlab.activities.services import create_activity_version, publish_activity_version
from linuxlab.assessment.models import Question, QuestionVersion
from linuxlab.assessment.providers import SingleChoiceProvider
from linuxlab.assessment.registry import QuestionRegistry
from linuxlab.assessment.services import create_question_version, publish_question_version
from linuxlab.content.models import LearningPathPage, LessonPage, ModulePage
from linuxlab.publication.models import PublicationSnapshot
from linuxlab.publication.package import (
    MANIFEST_MEMBER,
    MINIMUM_LINUXLAB_VERSION,
    PACKAGE_SCHEMA_VERSION,
    PAYLOAD_MEMBER,
    SIGNATURE_MEMBER,
    PackageBuildError,
    PackageManifest,
    PackageVerificationError,
    VerifiedPackage,
    build_package,
    verify_package,
)
from linuxlab.publication.signing import SIGNATURE_ALGORITHM, sign_bytes, verify_signature
from linuxlab.publication.snapshots import canonical_json_bytes, publish_learning_path

from ._wagtail_bootstrap import get_or_create_root_page

CONTENT = {
    "prompt": "Which command lists files?",
    "options": [{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
}
ANSWER_KEY = {"correct_option_id": "a"}
SCORING = {"max_score": 1}


class FakeProvider:
    """A provider with EXPLICIT, genuine no dependencies -- deliberately
    distinct from _BrokenProvider below, which simulates a provider that
    merely forgot to declare them (Task 11 final correction section 39:
    these two must behave differently)."""

    type_key = "fake"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": 1, "max_score": 1}

    def collect_dependencies(self, config):
        return ActivityDependencies.empty()


class _WorkingProvider:
    """A parametrizable provider with explicit empty dependencies, for
    tests that need a specific type_key not already used elsewhere."""

    def __init__(self, type_key):
        self.type_key = type_key

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": 1, "max_score": 1}

    def collect_dependencies(self, config):
        return ActivityDependencies.empty()


class _BrokenProvider:
    """Simulates exactly the bug this corrective closes: a registered
    provider that never implemented collect_dependencies() at all."""

    def __init__(self, type_key):
        self.type_key = type_key

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": 1, "max_score": 1}


def _fake_registry():
    reg = ActivityRegistry()
    reg.register(FakeProvider())
    return reg


@pytest.fixture
def snapshot(db):
    root = get_or_create_root_page()
    path = LearningPathPage(title="Linux Basics")
    root.add_child(instance=path)
    return publish_learning_path(path, _fake_registry())


@pytest.fixture
def keypair():
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


@pytest.fixture
def quiz_world(db):
    """The full real dependency chain: published Question -> published
    Assessment ActivityVersion -> Lesson activity reference -> published
    PublicationSnapshot -- exactly the Task 11 required integration path."""
    question_registry = QuestionRegistry()
    question_registry.register(SingleChoiceProvider())
    question = Question.objects.create(name="Shell basics Q1")
    question_version = create_question_version(
        question,
        type_key="single_choice",
        content=CONTENT,
        answer_key=ANSWER_KEY,
        scoring=SCORING,
    )
    published_question = publish_question_version(question_version, question_registry)

    from linuxlab.quiz.provider import AssessmentActivityProvider

    activity_registry = ActivityRegistry()
    activity_registry.register(AssessmentActivityProvider(question_registry))
    definition = ActivityDefinition.objects.create(name="Shell quiz", type_key="assessment")
    activity_version = create_activity_version(
        definition,
        config={
            "questions": [
                {
                    "question_public_id": str(published_question.question.public_id),
                    "question_version": published_question.version,
                }
            ]
        },
    )
    published_activity = publish_activity_version(activity_version, activity_registry)

    root = get_or_create_root_page()
    path = LearningPathPage(title="Linux Basics")
    root.add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")
    lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(definition.public_id),
                "activity_version": published_activity.version,
            },
        )
    ]
    module.add_child(instance=lesson)

    snapshot = publish_learning_path(path, activity_registry)

    return {
        "snapshot": snapshot,
        "activity_registry": activity_registry,
        "definition": definition,
        "activity_version": published_activity,
        "question": question,
        "question_version": published_question,
    }


def _publish_snapshot_with_activity(type_key, provider, config=None):
    """Publish a real LearningPath with one Lesson referencing one real
    published ActivityVersion of the given type_key/provider. Returns
    (snapshot, definition, activity_version, registry) where registry has
    ONLY that one provider registered."""
    registry = ActivityRegistry()
    registry.register(provider)

    definition = ActivityDefinition.objects.create(name=f"{type_key} activity", type_key=type_key)
    activity_version = create_activity_version(definition, config=config or {})
    published_activity = publish_activity_version(activity_version, registry)

    root = get_or_create_root_page()
    path = LearningPathPage(title="Linux Basics")
    root.add_child(instance=path)
    module = ModulePage(title="Module")
    path.add_child(instance=module)
    lesson = LessonPage(title="Lesson")
    lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(definition.public_id),
                "activity_version": published_activity.version,
            },
        )
    ]
    module.add_child(instance=lesson)

    snapshot = publish_learning_path(path, registry)
    return snapshot, definition, published_activity, registry


def _load_payload(verified):
    """Test-only convenience: independently parse the verified payload
    bytes into a plain dict for content assertions. This is explicitly the
    caller's own unauthoritative interpretation (Task 11 final correction
    section 44) -- never a second copy of VerifiedPackage's trust
    boundary, and mutating the result must never affect
    verified.payload_bytes itself."""
    return json.loads(verified.payload_bytes)


def _read_member(package_bytes, name):
    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        return archive.read(name)


def _rewrite_member(package_bytes, name, new_bytes):
    """Rebuild the archive with exactly one member's bytes replaced,
    otherwise preserving the other members untouched."""
    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    members[name] = new_bytes

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for member_name, data in members.items():
            archive.writestr(member_name, data)
    return buffer.getvalue()


def _resign_manifest_and_verify_rejected(manifest, package_bytes, private_key, registry, public_key):
    """Rebuild+re-sign a package around a modified manifest dict, so the
    check under test is isolated from an unrelated signature-mismatch
    failure (a real attacker who fully re-signs would still be blocked
    for lacking the trusted key; this isolates the OTHER failure mode: a
    legitimately-signed but structurally invalid manifest must still
    fail)."""
    payload_bytes = _read_member(package_bytes, PAYLOAD_MEMBER)
    manifest_bytes = canonical_json_bytes(manifest)
    signature = sign_bytes(manifest_bytes + payload_bytes, private_key)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, manifest_bytes)
        archive.writestr(PAYLOAD_MEMBER, payload_bytes)
        archive.writestr(SIGNATURE_MEMBER, signature)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), registry, public_key)


def _resign_payload_and_verify_rejected(payload, package_bytes, private_key, registry, public_key):
    """Rebuild+re-sign a package around a modified payload dict with a
    LEGITIMATE test private key -- proving the check under test is a real
    structural/closure rejection, not merely tamper detection (a real
    attacker without the trusted key would already be blocked by the
    signature alone)."""
    payload_bytes = canonical_json_bytes(payload)
    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["payload_sha256"] = sha256(payload_bytes).hexdigest()
    manifest_bytes = canonical_json_bytes(manifest)
    signature = sign_bytes(manifest_bytes + payload_bytes, private_key)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, manifest_bytes)
        archive.writestr(PAYLOAD_MEMBER, payload_bytes)
        archive.writestr(SIGNATURE_MEMBER, signature)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), registry, public_key)

    return buffer.getvalue()


def _corrupt_stored_content_hash(pk):
    """Simulate database corruption bypassing every Task 09 production
    guard -- a raw SQL UPDATE, never a supported write path."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE publication_publicationsnapshot SET content_hash = %s WHERE id = %s",
            ["0" * 64, pk],
        )


# --- required: real build + verify round trip with full closure ---------


@pytest.mark.django_db(transaction=True)
def test_build_package_from_a_real_publication_snapshot_verifies_with_the_matching_key(
    snapshot, keypair
):
    private_key, public_key = keypair
    registry = _fake_registry()

    package_bytes = build_package(snapshot, registry, private_key)
    verified = verify_package(package_bytes, registry, public_key)

    assert isinstance(verified, VerifiedPackage)
    manifest = verified.manifest
    assert isinstance(manifest, PackageManifest)
    assert manifest.schema_version == PACKAGE_SCHEMA_VERSION
    assert manifest.package_uuid == snapshot.source_public_id
    assert manifest.package_type == snapshot.kind
    assert manifest.package_version == snapshot.version
    assert manifest.minimum_linuxlab_version == MINIMUM_LINUXLAB_VERSION
    assert manifest.signature_algorithm == SIGNATURE_ALGORITHM
    assert isinstance(manifest.created_at, datetime)

    payload = _load_payload(verified)
    assert payload["publication"]["content_hash"] == snapshot.content_hash
    assert payload["publication"]["payload"] == snapshot.payload
    assert payload["activities"] == []
    assert payload["questions"] == []
    assert payload["server"]["questions"] == []


@pytest.mark.django_db(transaction=True)
def test_package_contains_exactly_the_three_v1_members(snapshot, keypair):
    private_key, _public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        names = sorted(archive.namelist())

    assert names == sorted([MANIFEST_MEMBER, PAYLOAD_MEMBER, SIGNATURE_MEMBER])


@pytest.mark.django_db(transaction=True)
def test_signature_member_is_a_raw_64_byte_ed25519_signature(snapshot, keypair):
    private_key, _public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    assert len(_read_member(package_bytes, SIGNATURE_MEMBER)) == 64


# --- required: real dependency closure integration ------------------------


@pytest.mark.django_db(transaction=True)
def test_full_closure_contains_activity_learner_question_and_server_grading(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)
    verified = verify_package(package_bytes, registry, public_key)
    payload = _load_payload(verified)

    activity_public_id = str(quiz_world["definition"].public_id)
    activity_version = quiz_world["activity_version"].version
    question_public_id = str(quiz_world["question"].public_id)
    question_version = quiz_world["question_version"].version

    # publication activity ref -> matching activity artifact exists
    assert payload["activities"] == [
        {
            "activity_public_id": activity_public_id,
            "version": activity_version,
            "type_key": "assessment",
            "config": {
                "questions": [
                    {
                        "question_public_id": question_public_id,
                        "question_version": question_version,
                    }
                ]
            },
        }
    ]

    # activity question ref -> matching learner question artifact exists
    assert payload["questions"] == [
        {
            "question_public_id": question_public_id,
            "version": question_version,
            "type_key": "single_choice",
            "content": CONTENT,
        }
    ]

    # assessment grading -> matching server question artifact exists
    assert payload["server"]["questions"] == [
        {
            "question_public_id": question_public_id,
            "version": question_version,
            "answer_key": ANSWER_KEY,
            "scoring": SCORING,
        }
    ]


@pytest.mark.django_db(transaction=True)
def test_duplicate_dependency_reference_appears_once_in_closure(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    # A second Lesson referencing the SAME exact ActivityVersion.
    live_path = LearningPathPage.objects.get(public_id=quiz_world["snapshot"].source_public_id)
    extra_module = ModulePage(title="Extra module")
    live_path.add_child(instance=extra_module)
    extra_lesson = LessonPage(title="Same quiz again")
    extra_lesson.body = [
        (
            "activity",
            {
                "activity_public_id": str(quiz_world["definition"].public_id),
                "activity_version": quiz_world["activity_version"].version,
            },
        )
    ]
    extra_module.add_child(instance=extra_lesson)

    republished = publish_learning_path(live_path, registry)

    package_bytes = build_package(republished, registry, private_key)
    verified = verify_package(package_bytes, registry, public_key)
    payload = _load_payload(verified)

    assert len(payload["activities"]) == 1
    assert len(payload["questions"]) == 1
    assert len(payload["server"]["questions"]) == 1


@pytest.mark.django_db(transaction=True)
def test_learner_sections_never_contain_answer_key_only_server_does(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]

    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)
    payload = _load_payload(verify_package(package_bytes, registry, public_key))

    learner_serialized = json.dumps(
        {
            "publication": payload["publication"],
            "activities": payload["activities"],
            "questions": payload["questions"],
        }
    )
    assert "answer_key" not in learner_serialized
    assert "correct_option_id" not in learner_serialized
    assert "scoring" not in learner_serialized

    server_serialized = json.dumps(payload["server"])
    assert "answer_key" in server_serialized
    assert "correct_option_id" in server_serialized


@pytest.mark.django_db(transaction=True)
def test_payload_publication_section_matches_source_snapshot_identity(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    payload = _load_payload(verify_package(package_bytes, registry, public_key))
    publication = payload["publication"]

    assert publication["kind"] == snapshot.kind
    assert publication["source_public_id"] == str(snapshot.source_public_id)
    assert publication["version"] == snapshot.version
    assert publication["content_hash"] == snapshot.content_hash
    assert publication["payload"] == snapshot.payload


# --- required: one-byte modification is rejected -------------------------


@pytest.mark.django_db(transaction=True)
def test_one_byte_modification_to_payload_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    payload_bytes = bytearray(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload_bytes[0] ^= 0xFF
    tampered = _rewrite_member(package_bytes, PAYLOAD_MEMBER, bytes(payload_bytes))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, registry, public_key)


# --- Important (independent review, Task 4 fix-round-1): malformed --------
# --- DEFLATE-compressed member data must not raise raw zlib.error --------


def _corrupt_deflate_member(package_bytes, member_name):
    """Corrupt a member's ON-DISK DEFLATE-compressed bytes in place,
    leaving the surrounding ZIP container structure (local header,
    filename/extra field lengths, compressed size) untouched -- so
    zipfile still recognizes the archive as well-formed, but decompressing
    this one member's data raises a genuine zlib.error, not a
    zipfile.BadZipFile. `_rewrite_member` (above) cannot reproduce this:
    rebuilding the archive with `writestr()` would either re-compress
    whatever bytes are given (producing valid DEFLATE of garbage content)
    or store them raw -- never inject an invalid DEFLATE byte stream under
    compression method 8, which is what a real corrupted/malicious archive
    looks like."""
    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        info = archive.getinfo(member_name)
    assert info.compress_type == zipfile.ZIP_DEFLATED

    header_offset = info.header_offset
    filename_len, extra_len = struct.unpack_from("<HH", package_bytes, header_offset + 26)
    data_start = header_offset + 30 + filename_len + extra_len
    compressed_size = info.compress_size

    corrupted = bytearray(package_bytes)
    # 0xFF's leading bits (BFINAL=1, BTYPE=11) are a reserved/invalid
    # DEFLATE block type, so inflate must fail on the very first block
    # header -- a deterministic, real zlib.error, not a probabilistic one.
    corrupted[data_start : data_start + compressed_size] = b"\xff" * compressed_size
    return bytes(corrupted)


@pytest.mark.django_db(transaction=True)
def test_malformed_deflate_member_is_rejected_as_packageverificationerror(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    corrupted = _corrupt_deflate_member(package_bytes, PAYLOAD_MEMBER)

    with pytest.raises(PackageVerificationError) as excinfo:
        verify_package(corrupted, registry, public_key)

    assert "zlib" not in str(excinfo.value).lower()


# --- Important (Corrective A final review, FA-I1): the remaining ----------
# --- untrusted-archive exception taxonomy zlib.error normalization -------
# --- (Task 4 fix-round-1) did not cover: encrypted members, unsupported ---
# --- compression methods, and corrupt BZIP2 streams ------------------------


_ZIP_ENCRYPTED_FLAG_BIT = 0x1  # ZIP general-purpose bit flag bit 0
_UNSUPPORTED_COMPRESSION_METHOD = 99  # not one of zipfile's supported methods


def _find_central_directory_offset(data, cd_start, member_name):
    """Locate the exact central directory record for `member_name`,
    searching only from `cd_start` onward (the real central directory
    start, from `ZipFile.start_dir`) so a coincidental "PK\\x01\\x02"
    byte sequence inside compressed member DATA can never be mistaken
    for a central directory record."""
    name_bytes = member_name.encode()
    search_from = cd_start
    while True:
        cd_offset = data.find(b"PK\x01\x02", search_from)
        if cd_offset == -1:
            raise AssertionError(f"central directory record for {member_name!r} not found")
        fn_len = struct.unpack_from("<H", data, cd_offset + 28)[0]
        candidate_name = bytes(data[cd_offset + 46 : cd_offset + 46 + fn_len])
        if candidate_name == name_bytes:
            return cd_offset
        search_from = cd_offset + 4


def _rewrite_member_with_encrypted_flag(package_bytes, member_name):
    """Mark a real member as encrypted via the exact real ZIP mechanism
    (general-purpose bit flag bit 0), in both the local file header and
    the central directory record -- this is what a genuinely encrypted
    ZIP member looks like on disk. Setting `ZipInfo.flag_bits` before
    `writestr()` does NOT work: zipfile recomputes flag bits itself
    during writing and discards whatever the caller set, so (like
    `_corrupt_deflate_member` above) this patches the raw bytes of an
    already-built real package directly."""
    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        info = archive.getinfo(member_name)
        cd_start = archive.start_dir

    data = bytearray(package_bytes)

    local_flag_offset = info.header_offset + 6
    local_flags = struct.unpack_from("<H", data, local_flag_offset)[0]
    struct.pack_into("<H", data, local_flag_offset, local_flags | _ZIP_ENCRYPTED_FLAG_BIT)

    cd_offset = _find_central_directory_offset(data, cd_start, member_name)
    cd_flag_offset = cd_offset + 8
    cd_flags = struct.unpack_from("<H", data, cd_flag_offset)[0]
    struct.pack_into("<H", data, cd_flag_offset, cd_flags | _ZIP_ENCRYPTED_FLAG_BIT)

    return bytes(data)


def _rewrite_member_with_unsupported_compression(package_bytes, member_name):
    """Mark a real member's compression method (in both the local file
    header and the central directory record) as a method number zipfile
    does not implement. The underlying compressed bytes are left
    untouched -- zipfile's decompressor dispatch fails purely on the
    method NUMBER, before ever attempting to interpret the bytes."""
    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        info = archive.getinfo(member_name)
        cd_start = archive.start_dir

    data = bytearray(package_bytes)

    local_method_offset = info.header_offset + 8
    struct.pack_into("<H", data, local_method_offset, _UNSUPPORTED_COMPRESSION_METHOD)

    cd_offset = _find_central_directory_offset(data, cd_start, member_name)
    cd_method_offset = cd_offset + 10
    struct.pack_into("<H", data, cd_method_offset, _UNSUPPORTED_COMPRESSION_METHOD)

    return bytes(data)


def _corrupt_bzip2_member(package_bytes, member_name):
    """Rebuild the archive with every member re-compressed as ZIP_BZIP2,
    then corrupt the target member's on-disk compressed byte stream --
    the other members' compression doesn't matter for this test, since
    verification fails while reading the corrupted member, long before
    any other member or the signature is ever checked."""
    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_BZIP2) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    rebuilt = bytearray(buffer.getvalue())

    with zipfile.ZipFile(BytesIO(bytes(rebuilt))) as archive:
        info = archive.getinfo(member_name)
    assert info.compress_type == zipfile.ZIP_BZIP2

    header_offset = info.header_offset
    filename_len, extra_len = struct.unpack_from("<HH", rebuilt, header_offset + 26)
    data_start = header_offset + 30 + filename_len + extra_len
    compressed_size = info.compress_size
    rebuilt[data_start : data_start + compressed_size] = b"\xff" * compressed_size

    return bytes(rebuilt)


@pytest.mark.django_db(transaction=True)
def test_encrypted_member_is_rejected_as_packageverificationerror(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    encrypted = _rewrite_member_with_encrypted_flag(package_bytes, PAYLOAD_MEMBER)

    with pytest.raises(PackageVerificationError) as excinfo:
        verify_package(encrypted, registry, public_key)

    assert "encrypted" in str(excinfo.value).lower()


@pytest.mark.django_db(transaction=True)
def test_unsupported_compression_member_is_rejected_as_packageverificationerror(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    corrupted = _rewrite_member_with_unsupported_compression(package_bytes, PAYLOAD_MEMBER)

    with pytest.raises(PackageVerificationError) as excinfo:
        verify_package(corrupted, registry, public_key)

    assert "compression" in str(excinfo.value).lower()


@pytest.mark.django_db(transaction=True)
def test_corrupt_bzip2_member_is_rejected_as_packageverificationerror(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    corrupted = _corrupt_bzip2_member(package_bytes, PAYLOAD_MEMBER)

    with pytest.raises(PackageVerificationError) as excinfo:
        verify_package(corrupted, registry, public_key)

    assert "zlib" not in str(excinfo.value).lower()


@pytest.mark.django_db(transaction=True)
def test_corrupt_lzma_member_is_rejected_as_packageverificationerror(snapshot, keypair):
    """Regression guard only (Section 11): a corrupted LZMA member
    already reaches PackageVerificationError on current HEAD, via
    zipfile's own CRC-32 check (LZMA's own decompressor tolerates the
    corrupted stream enough to decompress to unexpected content, so the
    rejection surfaces as a `zipfile.BadZipFile: Bad CRC-32` -- already
    inside the existing `except (zipfile.BadZipFile, ...)` clause) --
    pinned here so the FA-I1 exception-handling restructure cannot
    regress it."""
    with zipfile.ZipFile(BytesIO(build_package(snapshot, _fake_registry(), keypair[0]))) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_LZMA) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    rebuilt = bytearray(buffer.getvalue())

    with zipfile.ZipFile(BytesIO(bytes(rebuilt))) as archive:
        info = archive.getinfo(PAYLOAD_MEMBER)
    assert info.compress_type == zipfile.ZIP_LZMA

    header_offset = info.header_offset
    filename_len, extra_len = struct.unpack_from("<HH", rebuilt, header_offset + 26)
    data_start = header_offset + 30 + filename_len + extra_len
    compressed_size = info.compress_size
    rebuilt[data_start : data_start + compressed_size] = b"\xff" * compressed_size

    with pytest.raises(PackageVerificationError):
        verify_package(bytes(rebuilt), _fake_registry(), keypair[1])


# --- Critical: unsaved / mutated / hash-corrupt source --------------------


@pytest.mark.django_db(transaction=True)
def test_unsaved_snapshot_is_rejected():
    unsaved = PublicationSnapshot(
        kind="learning_path",
        source_public_id=uuid.uuid4(),
        version=1,
        payload={
            "kind": "learning_path",
            "learning_path": {"public_id": str(uuid.uuid4()), "title": "x", "modules": []},
        },
        content_hash="0" * 64,
    )

    private_key = Ed25519PrivateKey.generate()
    with pytest.raises(PackageBuildError):
        build_package(unsaved, _fake_registry(), private_key)

    assert (
        PublicationSnapshot.objects.filter(source_public_id=unsaved.source_public_id).count() == 0
    )


@pytest.mark.django_db(transaction=True)
def test_in_memory_mutated_payload_is_rejected(snapshot, keypair):
    private_key, _public_key = keypair

    snapshot.payload = {**snapshot.payload, "tampered": True}

    with pytest.raises(PackageBuildError):
        build_package(snapshot, _fake_registry(), private_key)


@pytest.mark.django_db(transaction=True)
def test_in_memory_mutated_version_is_rejected(snapshot, keypair):
    private_key, _public_key = keypair

    snapshot.version = snapshot.version + 1

    with pytest.raises(PackageBuildError):
        build_package(snapshot, _fake_registry(), private_key)


@pytest.mark.django_db(transaction=True)
def test_in_memory_mutated_kind_is_rejected(snapshot, keypair):
    private_key, _public_key = keypair

    snapshot.kind = "something_else"

    with pytest.raises(PackageBuildError):
        build_package(snapshot, _fake_registry(), private_key)


@pytest.mark.django_db(transaction=True)
def test_stored_content_hash_mismatch_is_rejected(snapshot, keypair):
    private_key, _public_key = keypair

    _corrupt_stored_content_hash(snapshot.pk)
    corrupted = PublicationSnapshot.objects.get(pk=snapshot.pk)

    with pytest.raises(PackageBuildError):
        build_package(corrupted, _fake_registry(), private_key)


# --- IMPORTANT: dependency closure failures fail closed --------------------


@pytest.mark.django_db(transaction=True)
def test_deleted_activity_version_after_publication_blocks_packaging(quiz_world, keypair):
    private_key, _public_key = keypair

    ActivityVersion.objects.filter(pk=quiz_world["activity_version"].pk).delete()

    with pytest.raises(PackageBuildError):
        build_package(quiz_world["snapshot"], quiz_world["activity_registry"], private_key)


@pytest.mark.django_db(transaction=True)
def test_deleted_question_version_after_publication_blocks_packaging(quiz_world, keypair):
    private_key, _public_key = keypair

    QuestionVersion.objects.filter(pk=quiz_world["question_version"].pk).delete()

    with pytest.raises(PackageBuildError):
        build_package(quiz_world["snapshot"], quiz_world["activity_registry"], private_key)


@pytest.mark.django_db(transaction=True)
def test_question_version_unpublished_via_raw_corruption_blocks_packaging(quiz_world, keypair):
    private_key, _public_key = keypair

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE assessment_questionversion SET published_at = NULL WHERE id = %s",
            [quiz_world["question_version"].pk],
        )

    with pytest.raises(PackageBuildError):
        build_package(quiz_world["snapshot"], quiz_world["activity_registry"], private_key)


# --- provider dependency contract: fail closed, never fail open ----------


@pytest.mark.django_db(transaction=True)
def test_build_with_provider_missing_collect_dependencies_fails_closed(keypair):
    private_key, _public_key = keypair
    snapshot, _definition, _activity_version, registry = _publish_snapshot_with_activity(
        "legacy_missing_deps", _BrokenProvider("legacy_missing_deps")
    )

    with pytest.raises(PackageBuildError):
        build_package(snapshot, registry, private_key)


@pytest.mark.django_db(transaction=True)
def test_verify_with_provider_missing_collect_dependencies_fails_closed(keypair):
    private_key, public_key = keypair
    snapshot, _definition, _activity_version, build_registry = _publish_snapshot_with_activity(
        "swappable", _WorkingProvider("swappable")
    )
    package_bytes = build_package(snapshot, build_registry, private_key)

    # A DIFFERENT registry supplied at verify time, whose provider for the
    # same type_key never implemented collect_dependencies() -- simulating
    # a real-world mismatch between what built the package and what later
    # verifies it.
    broken_registry = ActivityRegistry()
    broken_registry.register(_BrokenProvider("swappable"))

    with pytest.raises(PackageVerificationError):
        verify_package(package_bytes, broken_registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_explicit_empty_dependency_provider_builds_and_verifies_successfully(keypair):
    private_key, public_key = keypair
    snapshot, _definition, _activity_version, registry = _publish_snapshot_with_activity(
        "no_deps", _WorkingProvider("no_deps")
    )

    package_bytes = build_package(snapshot, registry, private_key)
    payload = _load_payload(verify_package(package_bytes, registry, public_key))

    assert len(payload["activities"]) == 1
    assert payload["questions"] == []
    assert payload["server"]["questions"] == []


@pytest.mark.django_db(transaction=True)
def test_build_with_unknown_provider_type_key_is_rejected(keypair):
    private_key, _public_key = keypair
    snapshot, _definition, _activity_version, _build_registry = _publish_snapshot_with_activity(
        "mystery", _WorkingProvider("mystery")
    )

    registry_without_mystery = _fake_registry()
    with pytest.raises(PackageBuildError):
        build_package(snapshot, registry_without_mystery, private_key)


@pytest.mark.django_db(transaction=True)
def test_verify_with_unknown_provider_type_key_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(
        quiz_world["snapshot"], quiz_world["activity_registry"], private_key
    )

    empty_registry = ActivityRegistry()  # no "assessment" provider registered

    with pytest.raises(PackageVerificationError):
        verify_package(package_bytes, empty_registry, public_key)


# --- adversarial suite: structure/signature --------------------------------


@pytest.mark.django_db(transaction=True)
def test_verification_with_the_wrong_public_key_is_rejected(snapshot, keypair):
    private_key, _public_key = keypair
    registry = _fake_registry()
    wrong_public_key = Ed25519PrivateKey.generate().public_key()

    package_bytes = build_package(snapshot, registry, private_key)

    with pytest.raises(PackageVerificationError):
        verify_package(package_bytes, registry, wrong_public_key)


@pytest.mark.django_db(transaction=True)
def test_tampered_manifest_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["package_version"] = manifest["package_version"] + 1
    tampered = _rewrite_member(package_bytes, MANIFEST_MEMBER, canonical_json_bytes(manifest))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_tampered_signature_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    signature = bytearray(_read_member(package_bytes, SIGNATURE_MEMBER))
    signature[0] ^= 0xFF
    tampered = _rewrite_member(package_bytes, SIGNATURE_MEMBER, bytes(signature))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_missing_member_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {n: archive.read(n) for n in archive.namelist() if n != SIGNATURE_MEMBER}

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_extra_unexpected_member_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr("extra.txt", b"not part of the schema")

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_duplicate_member_name_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr(MANIFEST_MEMBER, b'{"tampered": true}')

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_path_traversal_member_name_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {n: archive.read(n) for n in archive.namelist() if n != PAYLOAD_MEMBER}
        payload_bytes = archive.read(PAYLOAD_MEMBER)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr("../evil.json", payload_bytes)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), registry, public_key)


def test_malformed_zip_bytes_are_rejected():
    with pytest.raises(PackageVerificationError):
        verify_package(
            b"not a zip file at all", _fake_registry(), Ed25519PrivateKey.generate().public_key()
        )


@pytest.mark.django_db(transaction=True)
def test_non_json_manifest_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    tampered = _rewrite_member(package_bytes, MANIFEST_MEMBER, b"not json at all")

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_non_json_payload_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    # Re-sign over the corrupted payload so the failure under test is
    # specifically JSON-shape rejection, not signature mismatch.
    bogus_payload = b"not json at all"
    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["payload_sha256"] = sha256(bogus_payload).hexdigest()
    fixed_manifest_bytes = canonical_json_bytes(manifest)
    fixed_signature = sign_bytes(fixed_manifest_bytes + bogus_payload, private_key)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, fixed_manifest_bytes)
        archive.writestr(PAYLOAD_MEMBER, bogus_payload)
        archive.writestr(SIGNATURE_MEMBER, fixed_signature)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_unsupported_schema_version_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["schema_version"] = 999
    _resign_manifest_and_verify_rejected(manifest, package_bytes, private_key, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_boolean_schema_version_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["schema_version"] = True
    _resign_manifest_and_verify_rejected(manifest, package_bytes, private_key, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_manifest_missing_a_required_field_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    del manifest["minimum_linuxlab_version"]
    tampered = _rewrite_member(package_bytes, MANIFEST_MEMBER, canonical_json_bytes(manifest))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, registry, public_key)


# --- manifest/payload identity cross-check --------------------------------


@pytest.mark.django_db(transaction=True)
def test_manifest_identity_mismatch_with_payload_publication_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]
    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["publication"]["version"] = payload["publication"]["version"] + 1
    tampered_payload_bytes = canonical_json_bytes(payload)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["payload_sha256"] = sha256(tampered_payload_bytes).hexdigest()
    manifest_bytes = canonical_json_bytes(manifest)
    signature = sign_bytes(manifest_bytes + tampered_payload_bytes, private_key)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, manifest_bytes)
        archive.writestr(PAYLOAD_MEMBER, tampered_payload_bytes)
        archive.writestr(SIGNATURE_MEMBER, signature)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_embedded_publication_hash_mismatch_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]
    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["publication"]["content_hash"] = "f" * 64
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_orphan_server_grading_entry_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]
    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["server"]["questions"].append(
        {
            "question_public_id": payload["questions"][0]["question_public_id"],
            "version": payload["questions"][0]["version"] + 1,  # a different, unlisted version
            "answer_key": {"correct_option_id": "z"},
            "scoring": {"max_score": 1},
        }
    )
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_missing_server_grading_entry_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]
    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["server"]["questions"] = []
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_duplicate_activity_artifact_in_payload_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]
    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["activities"].append(dict(payload["activities"][0]))
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, registry, public_key)


# --- signed-but-incomplete: cryptographic authenticity is NOT enough -----


@pytest.mark.django_db(transaction=True)
def test_signed_package_missing_a_referenced_activity_artifact_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]
    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["activities"] = []
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_signed_package_missing_a_referenced_question_artifact_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]
    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["questions"] = []
    # Adjust server section too, so the PRE-EXISTING learner/server parity
    # check doesn't mask the NEW activity-derived closure check under test.
    payload["server"]["questions"] = []
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_signed_package_with_an_orphan_unreferenced_activity_artifact_is_rejected(
    quiz_world, keypair
):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]
    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    orphan = dict(payload["activities"][0])
    orphan["activity_public_id"] = str(uuid.uuid4())
    payload["activities"].append(orphan)
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, registry, public_key)


@pytest.mark.django_db(transaction=True)
def test_signed_package_with_an_orphan_unreferenced_question_artifact_is_rejected(
    quiz_world, keypair
):
    private_key, public_key = keypair
    registry = quiz_world["activity_registry"]
    package_bytes = build_package(quiz_world["snapshot"], registry, private_key)

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    orphan_question_id = str(uuid.uuid4())
    payload["questions"].append(
        {
            "question_public_id": orphan_question_id,
            "version": 1,
            "type_key": "single_choice",
            "content": CONTENT,
        }
    )
    # Kept in parity with `questions` so the PRE-EXISTING learner/server
    # parity check doesn't mask the NEW activity-derived closure check.
    payload["server"]["questions"].append(
        {
            "question_public_id": orphan_question_id,
            "version": 1,
            "answer_key": ANSWER_KEY,
            "scoring": SCORING,
        }
    )
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, registry, public_key)


# --- immutable VerifiedPackage trust boundary -----------------------------


@pytest.mark.django_db(transaction=True)
def test_verified_package_exposes_no_mutable_authoritative_payload(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    verified = verify_package(package_bytes, registry, public_key)

    assert isinstance(verified.package_bytes, bytes)
    assert isinstance(verified.payload_bytes, bytes)
    assert not hasattr(verified, "payload")

    with pytest.raises(dataclasses.FrozenInstanceError):
        verified.manifest = None

    with pytest.raises(dataclasses.FrozenInstanceError):
        verified.payload_bytes = b"{}"

    with pytest.raises(dataclasses.FrozenInstanceError):
        verified.package_bytes = b""


@pytest.mark.django_db(transaction=True)
def test_caller_parsed_payload_copy_does_not_affect_verified_bytes(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)
    verified = verify_package(package_bytes, registry, public_key)

    caller_copy = _load_payload(verified)
    caller_copy["activities"] = ["forged"]
    caller_copy["server"]["questions"] = ["forged"]

    # The trust boundary itself, re-inspected, is untouched by the
    # caller's own mutable interpretation.
    assert _load_payload(verified)["activities"] == []
    assert _load_payload(verified)["server"]["questions"] == []


@pytest.mark.django_db(transaction=True)
def test_verified_payload_bytes_are_exactly_the_signed_archived_bytes(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    verified = verify_package(package_bytes, registry, public_key)

    assert verified.payload_bytes == _read_member(package_bytes, PAYLOAD_MEMBER)

    # Independently confirm this is genuinely what the signature covers.
    manifest_bytes = _read_member(package_bytes, MANIFEST_MEMBER)
    signature = _read_member(package_bytes, SIGNATURE_MEMBER)
    verify_signature(manifest_bytes + verified.payload_bytes, signature, public_key)  # no raise


@pytest.mark.django_db(transaction=True)
def test_verified_package_bytes_equal_exact_input_bytes(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)

    verified = verify_package(package_bytes, registry, public_key)

    assert verified.package_bytes == package_bytes
    assert isinstance(verified.package_bytes, bytes)


@pytest.mark.django_db(transaction=True)
def test_bytearray_input_is_copied_so_later_mutation_does_not_affect_the_result(snapshot, keypair):
    private_key, public_key = keypair
    registry = _fake_registry()
    package_bytes = build_package(snapshot, registry, private_key)
    mutable_input = bytearray(package_bytes)

    verified = verify_package(mutable_input, registry, public_key)

    # Mutate the caller's own buffer AFTER verification.
    mutable_input[0] ^= 0xFF

    assert verified.package_bytes == package_bytes
    assert isinstance(verified.package_bytes, bytes)


# --- determinism -----------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_publications_of_the_same_source_preserve_distinct_package_versions(keypair):
    private_key, public_key = keypair
    root = get_or_create_root_page()
    path = LearningPathPage(title="Linux Basics")
    root.add_child(instance=path)
    registry = _fake_registry()

    first = publish_learning_path(path, registry)
    second = publish_learning_path(path, registry)
    assert first.content_hash == second.content_hash
    assert first.version != second.version

    first_verified = verify_package(build_package(first, registry, private_key), registry, public_key)
    second_verified = verify_package(
        build_package(second, registry, private_key), registry, public_key
    )

    assert first_verified.manifest.package_version == first.version
    assert second_verified.manifest.package_version == second.version
    assert first_verified.manifest.package_version != second_verified.manifest.package_version
