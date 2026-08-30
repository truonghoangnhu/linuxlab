"""Task 13 HTTP adapter: GET /learn/<uuid:source_public_id>/ -- a thin
Node-only route over the linuxlab.runtime core (T13-D15). The view must
never resolve Installed* directly, never fall back to Central/Wagtail/
latest-version data, and never expose grading material."""

import uuid

import pytest
from django.test import Client, override_settings

from linuxlab.publication.package import build_package
from linuxlab.sync.services import activate_package, import_package

from .conftest import republish

pytestmark = pytest.mark.django_db(transaction=True)

_NODE_SETTINGS = {
    "LINUXLAB_CAN_IMPORT_PACKAGES": True,
    "LINUXLAB_CAN_SERVE_LEARNER_CONTENT": True,
}


def _stage(world, private_key, public_key):
    package_bytes = build_package(world["snapshot"], world["activity_registry"], private_key)
    return import_package(package_bytes, world["activity_registry"], public_key)


@override_settings(**_NODE_SETTINGS)
def test_learn_route_renders_active_installed_publication(quiz_world, keypair):
    private_key, public_key = keypair
    package = _stage(quiz_world, private_key, public_key)
    activate_package(package)

    client = Client()
    response = client.get(f"/learn/{quiz_world['snapshot'].source_public_id}/")

    assert response.status_code == 200
    assert response.context["runtime_context"].package_version == package.package_version
    assert "Linux Basics" in response.content.decode()


@override_settings(**_NODE_SETTINGS)
def test_learn_route_never_uses_a_staged_higher_version(quiz_world, keypair):
    private_key, public_key = keypair
    v1 = _stage(quiz_world, private_key, public_key)
    activate_package(v1)

    v2_snapshot = republish(quiz_world)
    v2_bytes = build_package(v2_snapshot, quiz_world["activity_registry"], private_key)
    v2 = import_package(v2_bytes, quiz_world["activity_registry"], public_key)
    assert v2.package_version > v1.package_version  # staged, never activated

    client = Client()
    response = client.get(f"/learn/{quiz_world['snapshot'].source_public_id}/")

    assert response.context["runtime_context"].package_version == v1.package_version


@override_settings(**_NODE_SETTINGS)
def test_no_active_package_returns_offline_unavailable_response():
    client = Client()
    response = client.get(f"/learn/{uuid.uuid4()}/")

    assert response.status_code == 503


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True, LINUXLAB_CAN_SERVE_LEARNER_CONTENT=False)
def test_central_profile_rejects_the_learner_route(quiz_world, keypair):
    private_key, public_key = keypair
    package = _stage(quiz_world, private_key, public_key)
    activate_package(package)

    client = Client()
    response = client.get(f"/learn/{quiz_world['snapshot'].source_public_id}/")

    assert response.status_code == 403


@override_settings(**_NODE_SETTINGS)
def test_learner_route_response_has_no_grading_material(quiz_world, keypair):
    private_key, public_key = keypair
    package = _stage(quiz_world, private_key, public_key)
    activate_package(package)

    client = Client()
    response = client.get(f"/learn/{quiz_world['snapshot'].source_public_id}/")

    rendered = response.content.decode()
    assert "answer_key" not in rendered
    assert "correct_option_id" not in rendered
    assert "scoring" not in rendered


@override_settings(**_NODE_SETTINGS)
def test_switching_active_package_affects_only_new_requests(quiz_world, keypair):
    """A response already rendered from v1 is unaffected by a later
    activation; a subsequent request picks up the new ACTIVE package."""
    private_key, public_key = keypair
    v1 = _stage(quiz_world, private_key, public_key)
    activate_package(v1)

    client = Client()
    first_response = client.get(f"/learn/{quiz_world['snapshot'].source_public_id}/")
    assert first_response.context["runtime_context"].package_version == v1.package_version

    v2_snapshot = republish(quiz_world)
    v2 = import_package(
        build_package(v2_snapshot, quiz_world["activity_registry"], private_key),
        quiz_world["activity_registry"],
        public_key,
    )
    activate_package(v2)

    second_response = client.get(f"/learn/{quiz_world['snapshot'].source_public_id}/")
    assert second_response.context["runtime_context"].package_version == v2.package_version
