"""Task 11: real PublicationSnapshot -> signed .llpkg package -> verify.

No mocks for the core path: a real Task 09 PublicationSnapshot is the sole
package source, and a real ephemeral Ed25519 keypair signs/verifies it."""

import json
import zipfile
from datetime import datetime
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from linuxlab.activities.registry import ActivityRegistry
from linuxlab.content.models import LearningPathPage
from linuxlab.publication.package import (
    MANIFEST_MEMBER,
    MINIMUM_LINUXLAB_VERSION,
    PACKAGE_SCHEMA_VERSION,
    PAYLOAD_MEMBER,
    SIGNATURE_MEMBER,
    PackageManifest,
    PackageVerificationError,
    build_package,
    verify_package,
)
from linuxlab.publication.signing import SIGNATURE_ALGORITHM
from linuxlab.publication.snapshots import canonical_json_bytes, publish_learning_path

from ._wagtail_bootstrap import get_or_create_root_page


class FakeProvider:
    type_key = "fake"

    def validate_config(self, config):
        pass

    def evaluate(self, config, submission):
        return {"score": 1, "max_score": 1}


def _registry():
    reg = ActivityRegistry()
    reg.register(FakeProvider())
    return reg


@pytest.fixture
def snapshot(db):
    root = get_or_create_root_page()
    path = LearningPathPage(title="Linux Basics")
    root.add_child(instance=path)
    return publish_learning_path(path, _registry())


@pytest.fixture
def keypair():
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


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


# --- required: real build + verify round trip ---------------------------


@pytest.mark.django_db(transaction=True)
def test_build_package_from_a_real_publication_snapshot_verifies_with_the_matching_key(
    snapshot, keypair
):
    private_key, public_key = keypair

    package_bytes = build_package(snapshot, private_key)
    manifest = verify_package(package_bytes, public_key)

    assert isinstance(manifest, PackageManifest)
    assert manifest.schema_version == PACKAGE_SCHEMA_VERSION
    assert manifest.package_uuid == snapshot.source_public_id
    assert manifest.package_type == snapshot.kind
    assert manifest.package_version == snapshot.version
    assert manifest.payload_sha256 == snapshot.content_hash
    assert manifest.minimum_linuxlab_version == MINIMUM_LINUXLAB_VERSION
    assert manifest.signature_algorithm == SIGNATURE_ALGORITHM
    assert isinstance(manifest.created_at, datetime)


@pytest.mark.django_db(transaction=True)
def test_package_contains_exactly_the_three_v1_members(snapshot, keypair):
    private_key, _public_key = keypair

    package_bytes = build_package(snapshot, private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        names = sorted(archive.namelist())

    assert names == sorted([MANIFEST_MEMBER, PAYLOAD_MEMBER, SIGNATURE_MEMBER])


@pytest.mark.django_db(transaction=True)
def test_payload_member_is_the_snapshots_own_canonical_payload_bytes(snapshot, keypair):
    private_key, _public_key = keypair

    package_bytes = build_package(snapshot, private_key)

    payload_bytes = _read_member(package_bytes, PAYLOAD_MEMBER)
    assert payload_bytes == canonical_json_bytes(snapshot.payload)


@pytest.mark.django_db(transaction=True)
def test_signature_member_is_a_raw_64_byte_ed25519_signature(snapshot, keypair):
    private_key, _public_key = keypair

    package_bytes = build_package(snapshot, private_key)

    signature_bytes = _read_member(package_bytes, SIGNATURE_MEMBER)
    assert len(signature_bytes) == 64


@pytest.mark.django_db(transaction=True)
def test_two_publications_of_the_same_source_preserve_distinct_package_versions(keypair):
    private_key, public_key = keypair
    root = get_or_create_root_page()
    path = LearningPathPage(title="Linux Basics")
    root.add_child(instance=path)
    registry = _registry()

    first = publish_learning_path(path, registry)
    second = publish_learning_path(path, registry)
    assert first.content_hash == second.content_hash  # unchanged content
    assert first.version != second.version

    first_package = verify_package(build_package(first, private_key), public_key)
    second_package = verify_package(build_package(second, private_key), public_key)

    assert first_package.package_version == first.version
    assert second_package.package_version == second.version
    assert first_package.package_version != second_package.package_version
    # content-addressed digest matches even though package identity (version) differs
    assert first_package.payload_sha256 == second_package.payload_sha256


# --- required: one-byte modification is rejected -------------------------


@pytest.mark.django_db(transaction=True)
def test_one_byte_modification_to_payload_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, private_key)

    payload_bytes = bytearray(_read_member(package_bytes, PAYLOAD_MEMBER))
    payload_bytes[0] ^= 0xFF
    tampered = _rewrite_member(package_bytes, PAYLOAD_MEMBER, bytes(payload_bytes))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, public_key)


# --- adversarial suite ----------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_verification_with_the_wrong_public_key_is_rejected(snapshot, keypair):
    private_key, _public_key = keypair
    wrong_public_key = Ed25519PrivateKey.generate().public_key()

    package_bytes = build_package(snapshot, private_key)

    with pytest.raises(PackageVerificationError):
        verify_package(package_bytes, wrong_public_key)


@pytest.mark.django_db(transaction=True)
def test_tampered_manifest_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["package_version"] = manifest["package_version"] + 1
    tampered = _rewrite_member(
        package_bytes, MANIFEST_MEMBER, canonical_json_bytes(manifest)
    )

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, public_key)


@pytest.mark.django_db(transaction=True)
def test_tampered_signature_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, private_key)

    signature = bytearray(_read_member(package_bytes, SIGNATURE_MEMBER))
    signature[0] ^= 0xFF
    tampered = _rewrite_member(package_bytes, SIGNATURE_MEMBER, bytes(signature))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, public_key)


@pytest.mark.django_db(transaction=True)
def test_missing_member_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, private_key)

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
    package_bytes = build_package(snapshot, private_key)

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
    package_bytes = build_package(snapshot, private_key)

    with zipfile.ZipFile(BytesIO(package_bytes)) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        # duplicate the manifest member under the same name with different bytes
        archive.writestr(MANIFEST_MEMBER, b'{"tampered": true}')

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), public_key)


@pytest.mark.django_db(transaction=True)
def test_path_traversal_member_name_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, private_key)

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
    package_bytes = build_package(snapshot, private_key)

    tampered = _rewrite_member(package_bytes, MANIFEST_MEMBER, b"not json at all")

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, public_key)


@pytest.mark.django_db(transaction=True)
def test_unsupported_schema_version_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    manifest["schema_version"] = 999
    tampered_manifest_bytes = canonical_json_bytes(manifest)

    # Sign with a matching bogus manifest so we isolate the schema-version
    # check from signature verification (a real attacker who fully
    # resigned the package would still be blocked because they don't hold
    # the trusted private key -- this test isolates the OTHER failure mode:
    # a legitimately-signed but unsupported schema version must still fail).
    from linuxlab.publication.signing import sign_bytes

    payload_bytes = _read_member(package_bytes, PAYLOAD_MEMBER)
    new_signature = sign_bytes(tampered_manifest_bytes + payload_bytes, private_key)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, tampered_manifest_bytes)
        archive.writestr(PAYLOAD_MEMBER, payload_bytes)
        archive.writestr(SIGNATURE_MEMBER, new_signature)

    with pytest.raises(PackageVerificationError):
        verify_package(buffer.getvalue(), public_key)


@pytest.mark.django_db(transaction=True)
def test_manifest_missing_a_required_field_is_rejected(snapshot, keypair):
    private_key, public_key = keypair
    package_bytes = build_package(snapshot, private_key)

    manifest = json.loads(_read_member(package_bytes, MANIFEST_MEMBER))
    del manifest["minimum_linuxlab_version"]
    tampered = _rewrite_member(package_bytes, MANIFEST_MEMBER, canonical_json_bytes(manifest))

    with pytest.raises(PackageVerificationError):
        verify_package(tampered, public_key)


@pytest.mark.django_db(transaction=True)
def test_verified_package_never_leaks_an_answer_key(keypair):
    private_key, public_key = keypair
    from linuxlab.activities.models import ActivityDefinition
    from linuxlab.activities.services import create_activity_version, publish_activity_version
    from linuxlab.assessment.models import Question
    from linuxlab.assessment.providers import SingleChoiceProvider
    from linuxlab.assessment.registry import QuestionRegistry
    from linuxlab.assessment.services import create_question_version, publish_question_version
    from linuxlab.content.models import LessonPage, ModulePage
    from linuxlab.quiz.provider import AssessmentActivityProvider

    root = get_or_create_root_page()
    path = LearningPathPage(title="Linux Basics")
    root.add_child(instance=path)
    module = ModulePage(title="Shell Basics")
    path.add_child(instance=module)
    lesson = LessonPage(title="The ls command")

    question_registry = QuestionRegistry()
    question_registry.register(SingleChoiceProvider())
    question = Question.objects.create(name="Shell basics Q1")
    question_version = create_question_version(
        question,
        type_key="single_choice",
        content={
            "prompt": "Which command lists files?",
            "options": [{"id": "a", "text": "ls"}, {"id": "b", "text": "rm"}],
        },
        answer_key={"correct_option_id": "a"},
        scoring={"max_score": 1},
    )
    published_question = publish_question_version(question_version, question_registry)

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

    quiz_snapshot = publish_learning_path(path, activity_registry)

    package_bytes = build_package(quiz_snapshot, private_key)
    verify_package(package_bytes, public_key)

    payload_bytes = _read_member(package_bytes, PAYLOAD_MEMBER)
    manifest_bytes = _read_member(package_bytes, MANIFEST_MEMBER)
    serialized = (payload_bytes + manifest_bytes).decode("utf-8")

    assert "answer_key" not in serialized
    assert "correct_option_id" not in serialized
    assert "scoring" not in serialized
