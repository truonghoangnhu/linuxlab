"""Task 11 corrective: real PublicationSnapshot + real Activity/Question
dependency closure -> self-contained signed .llpkg -> verify.

No mocks for the core path: a real Task 09 PublicationSnapshot, a real
published Assessment ActivityVersion, a real published QuestionVersion, and
a real ephemeral Ed25519 keypair build and verify every package here."""

import json
import zipfile
from datetime import datetime
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.db import connection

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
from linuxlab.publication.signing import SIGNATURE_ALGORITHM
from linuxlab.publication.snapshots import canonical_json_bytes, publish_learning_path

from ._wagtail_bootstrap import get_or_create_root_page

CONTENT = {
    "prompt": "Which command lists files?",
    "options": [{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
}
ANSWER_KEY = {"correct_option_id": "a"}
SCORING = {"max_score": 1}


class FakeProvider:
    type_key = "fake"

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


def _corrupt_stored_content_hash(pk):
    """Simulate database corruption bypassing every Task 09 production
    guard -- a raw SQL UPDATE, never a supported write path -- so the
    corrective's stored-hash-enforcement check can be tested in isolation
    (Task 11 corrective section 45)."""
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
    verified = verify_package(package_bytes, public_key)

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

    assert verified.payload["publication"]["content_hash"] == snapshot.content_hash
    assert verified.payload["publication"]["payload"] == snapshot.payload
    assert verified.payload["activities"] == []
    assert verified.payload["questions"] == []
    assert verified.payload["server"]["questions"] == []


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


# --- required: real dependency closure integration (section 41/24) -------


@pytest.mark.django_db(transaction=True)
def test_full_closure_contains_activity_learner_question_and_server_grading(quiz_world, keypair):
    private_key, public_key = keypair

    package_bytes = build_package(
        quiz_world["snapshot"], quiz_world["activity_registry"], private_key
    )
    verified = verify_package(package_bytes, public_key)
    payload = verified.payload

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

    republished = publish_learning_path(live_path, quiz_world["activity_registry"])

    package_bytes = build_package(republished, quiz_world["activity_registry"], private_key)
    verified = verify_package(package_bytes, public_key)

    assert len(verified.payload["activities"]) == 1
    assert len(verified.payload["questions"]) == 1
    assert len(verified.payload["server"]["questions"]) == 1


@pytest.mark.django_db(transaction=True)
def test_learner_sections_never_contain_answer_key_only_server_does(quiz_world, keypair):
    private_key, public_key = keypair

    package_bytes = build_package(
        quiz_world["snapshot"], quiz_world["activity_registry"], private_key
    )
    verified = verify_package(package_bytes, public_key)
    payload = verified.payload

    learner_serialized = json.dumps(
        {"publication": payload["publication"], "activities": payload["activities"], "questions": payload["questions"]}
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
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    verified = verify_package(package_bytes, public_key)
    publication = verified.payload["publication"]

    assert publication["kind"] == snapshot.kind
    assert publication["source_public_id"] == str(snapshot.source_public_id)
    assert publication["version"] == snapshot.version
    assert publication["content_hash"] == snapshot.content_hash
    assert publication["payload"] == snapshot.payload


# --- required: one-byte modification is rejected -------------------------


@pytest.mark.django_db(transaction=True)
def test_one_byte_modification_to_payload_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    payload_bytes = bytearray(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload_bytes[0] ^= 0xFF
    tampered = _rewrite_member(package_bytes, PAYLOAD_MEMBER, bytes(payload_bytes))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, public_key)


# --- Critical: unsaved / mutated / hash-corrupt source --------------------


@pytest.mark.django_db(transaction=True)
def test_unsaved_snapshot_is_rejected():
    import uuid as uuid_mod

    unsaved = PublicationSnapshot(
        kind="learning_path",
        source_public_id=uuid_mod.uuid4(),
        version=1,
        payload={"kind": "learning_path", "learning_path": {"public_id": str(uuid_mod.uuid4()), "title": "x", "modules": []}},
        content_hash="0" * 64,
    )

    private_key = Ed25519PrivateKey.generate()
    with pytest.raises(PackageBuildError):
        build_package(unsaved, _fake_registry(), private_key)

    assert PublicationSnapshot.objects.filter(source_public_id=unsaved.source_public_id).count() == 0


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
        build_package(
            quiz_world["snapshot"], quiz_world["activity_registry"], private_key
        )


@pytest.mark.django_db(transaction=True)
def test_deleted_question_version_after_publication_blocks_packaging(quiz_world, keypair):
    private_key, _public_key = keypair

    QuestionVersion.objects.filter(pk=quiz_world["question_version"].pk).delete()

    with pytest.raises(PackageBuildError):
        build_package(
            quiz_world["snapshot"], quiz_world["activity_registry"], private_key
        )


@pytest.mark.django_db(transaction=True)
def test_question_version_unpublished_via_raw_corruption_blocks_packaging(quiz_world, keypair):
    private_key, _public_key = keypair

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE assessment_questionversion SET published_at = NULL WHERE id = %s",
            [quiz_world["question_version"].pk],
        )

    with pytest.raises(PackageBuildError):
        build_package(
            quiz_world["snapshot"], quiz_world["activity_registry"], private_key
        )


# --- adversarial suite: structure/signature --------------------------------


@pytest.mark.django_db(transaction=True)
def test_verification_with_the_wrong_public_key_is_rejected(snapshot, keypair):
    private_key, _public_key = keypair
    wrong_public_key = Ed25519PrivateKey.generate().public_key()

    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    with pytest.raises(PackageVerificationError):
        verify_package(package_bytes, wrong_public_key)


@pytest.mark.django_db(transaction=True)
def test_tampered_manifest_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["package_version"] = manifest["package_version"] + 1
    tampered = _rewrite_member(package_bytes, MANIFEST_MEMBER, canonical_json_bytes(manifest))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, public_key)


@pytest.mark.django_db(transaction=True)
def test_tampered_signature_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    signature = bytearray(_read_member(package_bytes, SIGNATURE_MEMBER))
    signature[0] ^= 0xFF
    tampered = _rewrite_member(package_bytes, SIGNATURE_MEMBER, bytes(signature))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, public_key)


@pytest.mark.django_db(transaction=True)
def test_missing_member_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {n: archive.read(n) for n in archive.namelist() if n != SIGNATURE_MEMBER}

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), public_key)


@pytest.mark.django_db(transaction=True)
def test_extra_unexpected_member_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr("extra.txt", b"not part of the schema")

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), public_key)


@pytest.mark.django_db(transaction=True)
def test_duplicate_member_name_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr(MANIFEST_MEMBER, b'{"tampered": true}')

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), public_key)


@pytest.mark.django_db(transaction=True)
def test_path_traversal_member_name_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {n: archive.read(n) for n in archive.namelist() if n != PAYLOAD_MEMBER}
        payload_bytes = archive.read(PAYLOAD_MEMBER)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr("../evil.json", payload_bytes)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), public_key)


def test_malformed_zip_bytes_are_rejected():
    with pytest.raises(PackageVerificationError):
        verify_package(b"not a zip file at all", Ed25519PrivateKey.generate().public_key())


@pytest.mark.django_db(transaction=True)
def test_non_json_manifest_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    tampered = _rewrite_member(package_bytes, MANIFEST_MEMBER, b"not json at all")

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, public_key)


@pytest.mark.django_db(transaction=True)
def test_non_json_payload_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    # Re-sign over the corrupted payload so the failure under test is
    # specifically JSON-shape rejection, not signature mismatch.
    from linuxlab.publication.signing import sign_bytes

    bogus_payload = b"not json at all"
    manifest_bytes = _read_member(package_bytes, MANIFEST_MEMBER)
    new_signature = sign_bytes(manifest_bytes + bogus_payload, private_key)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, manifest_bytes)
        archive.writestr(PAYLOAD_MEMBER, bogus_payload)
        archive.writestr(SIGNATURE_MEMBER, new_signature)

    # The manifest's payload_sha256 no longer matches, which is itself a
    # correct rejection reason -- but to specifically prove "invalid JSON
    # is rejected" rather than "digest mismatch is rejected", rebuild the
    # manifest's payload_sha256 to match the bogus bytes too.
    manifest = json.loads(manifest_bytes)
    from hashlib import sha256

    manifest["payload_sha256"] = sha256(bogus_payload).hexdigest()
    fixed_manifest_bytes = canonical_json_bytes(manifest)
    fixed_signature = sign_bytes(fixed_manifest_bytes + bogus_payload, private_key)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, fixed_manifest_bytes)
        archive.writestr(PAYLOAD_MEMBER, bogus_payload)
        archive.writestr(SIGNATURE_MEMBER, fixed_signature)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), public_key)


@pytest.mark.django_db(transaction=True)
def test_unsupported_schema_version_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["schema_version"] = 999
    _resign_manifest_and_verify_rejected(manifest, package_bytes, private_key, public_key)


@pytest.mark.django_db(transaction=True)
def test_boolean_schema_version_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["schema_version"] = True
    _resign_manifest_and_verify_rejected(manifest, package_bytes, private_key, public_key)


def _resign_manifest_and_verify_rejected(manifest, package_bytes, private_key, public_key):
    """Rebuild+re-sign a package around a modified manifest dict, so the
    check under test is isolated from an unrelated signature-mismatch
    failure (a real attacker who fully re-signs would still be blocked
    for lacking the trusted key; this isolates the OTHER failure mode: a
    legitimately-signed but structurally invalid manifest must still
    fail)."""
    from linuxlab.publication.signing import sign_bytes

    payload_bytes = _read_member(package_bytes, PAYLOAD_MEMBER)
    manifest_bytes = canonical_json_bytes(manifest)
    signature = sign_bytes(manifest_bytes + payload_bytes, private_key)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, manifest_bytes)
        archive.writestr(PAYLOAD_MEMBER, payload_bytes)
        archive.writestr(SIGNATURE_MEMBER, signature)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), public_key)


@pytest.mark.django_db(transaction=True)
def test_manifest_missing_a_required_field_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, _fake_registry(), private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    del manifest["minimum_linuxlab_version"]
    tampered = _rewrite_member(package_bytes, MANIFEST_MEMBER, canonical_json_bytes(manifest))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, public_key)


# --- manifest/payload identity cross-check --------------------------------


@pytest.mark.django_db(transaction=True)
def test_manifest_identity_mismatch_with_payload_publication_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(
        quiz_world["snapshot"], quiz_world["activity_registry"], private_key
    )

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["publication"]["version"] = payload["publication"]["version"] + 1
    tampered_payload_bytes = canonical_json_bytes(payload)

    from hashlib import sha256

    from linuxlab.publication.signing import sign_bytes

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
        verify_package(buffer.getvalue(), public_key)


@pytest.mark.django_db(transaction=True)
def test_embedded_publication_hash_mismatch_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(
        quiz_world["snapshot"], quiz_world["activity_registry"], private_key
    )

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["publication"]["content_hash"] = "f" * 64
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, public_key)


@pytest.mark.django_db(transaction=True)
def test_orphan_server_grading_entry_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(
        quiz_world["snapshot"], quiz_world["activity_registry"], private_key
    )

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["server"]["questions"].append(
        {
            "question_public_id": payload["questions"][0]["question_public_id"],
            "version": payload["questions"][0]["version"] + 1,  # a different, unlisted version
            "answer_key": {"correct_option_id": "z"},
            "scoring": {"max_score": 1},
        }
    )
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, public_key)


@pytest.mark.django_db(transaction=True)
def test_missing_server_grading_entry_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(
        quiz_world["snapshot"], quiz_world["activity_registry"], private_key
    )

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["server"]["questions"] = []
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, public_key)


@pytest.mark.django_db(transaction=True)
def test_duplicate_activity_artifact_in_payload_is_rejected(quiz_world, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(
        quiz_world["snapshot"], quiz_world["activity_registry"], private_key
    )

    payload = json.loads(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload["activities"].append(dict(payload["activities"][0]))
    _resign_payload_and_verify_rejected(payload, package_bytes, private_key, public_key)


def _resign_payload_and_verify_rejected(payload, package_bytes, private_key, public_key):
    from hashlib import sha256

    from linuxlab.publication.signing import sign_bytes

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
        verify_package(buffer.getvalue(), public_key)


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

    first_verified = verify_package(build_package(first, registry, private_key), public_key)
    second_verified = verify_package(build_package(second, registry, private_key), public_key)

    assert first_verified.manifest.package_version == first.version
    assert second_verified.manifest.package_version == second.version
    assert first_verified.manifest.package_version != second_verified.manifest.package_version
