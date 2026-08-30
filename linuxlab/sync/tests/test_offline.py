"""Task 13 offline requirement: the Node LAN learner route must serve
installed ACTIVE content with zero outbound network access. Outbound
socket entry points are patched to fail loudly; the in-process Django
test client and the already-open PostgreSQL connection (psycopg's C
extension talks to libpq directly, not through Python's `socket`
module) are unaffected."""

import socket

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import Client, override_settings

from linuxlab.publication.package import build_package
from linuxlab.sync.services import activate_package, import_package

pytestmark = pytest.mark.django_db(transaction=True)

_NODE_SETTINGS = {
    "LINUXLAB_CAN_IMPORT_PACKAGES": True,
    "LINUXLAB_CAN_SERVE_LEARNER_CONTENT": True,
}


def _deny_network(*args, **kwargs):
    raise AssertionError("Task 13 runtime/HTTP path attempted outbound network access")


@override_settings(**_NODE_SETTINGS)
def test_learn_route_serves_active_content_with_outbound_network_blocked(monkeypatch, quiz_world):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    package_bytes = build_package(quiz_world["snapshot"], quiz_world["activity_registry"], private_key)
    package = import_package(package_bytes, quiz_world["activity_registry"], public_key)
    activate_package(package)

    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)

    client = Client()
    response = client.get(f"/learn/{quiz_world['snapshot'].source_public_id}/")

    assert response.status_code == 200
    assert "Linux Basics" in response.content.decode()
