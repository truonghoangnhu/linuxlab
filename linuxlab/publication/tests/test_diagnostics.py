"""Corrective B Task 10: PublicationDiagnosticProvider owns SIGNING_KEY,
required only under LINUXLAB_CAN_AUTHOR, over the unchanged fixed-path
load_package_signing_private_key() loader. Structural validity only --
never a remote trust comparison, never key/path/PEM leakage."""

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from linuxlab.core.diagnostics import DiagnosticContext, report_to_json
from linuxlab.publication.diagnostics import PublicationDiagnosticProvider

AUTHOR = DiagnosticContext("central", "local", frozenset({"LINUXLAB_CAN_AUTHOR"}))
NO_AUTHOR = DiagnosticContext("central", "local", frozenset())
NODE = DiagnosticContext("node", "local", frozenset({"LINUXLAB_CAN_IMPORT_PACKAGES"}))


def _private_pem(key=None):
    key = key or Ed25519PrivateKey.generate()
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def _install_signing_key(monkeypatch, tmp_path, pem_bytes):
    key_path = tmp_path / "package-signing.key"
    key_path.write_bytes(pem_bytes)
    monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)
    return key_path


def _rendered(checks):
    return " ".join(f"{c.code} {c.status} {c.message} {dict(c.details)!r} {c!r}" for c in checks)


def test_provider_owns_signing_key_only_when_authoring_is_enabled():
    provider = PublicationDiagnosticProvider()
    assert provider.codes == frozenset({"SIGNING_KEY"})
    assert provider.required_codes(AUTHOR) == frozenset({"SIGNING_KEY"})
    assert provider.required_codes(NO_AUTHOR) == frozenset()
    assert provider.required_codes(NODE) == frozenset()


def test_provider_emits_nothing_and_reads_no_key_when_not_required(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "linuxlab.publication.signing._SIGNING_KEY_PATH", tmp_path / "must-not-be-opened.key"
    )
    calls = []
    monkeypatch.setattr(
        "linuxlab.publication.diagnostics.load_package_signing_private_key",
        lambda: calls.append(1),
    )
    assert PublicationDiagnosticProvider().checks(NO_AUTHOR) == ()
    assert PublicationDiagnosticProvider().checks(NODE) == ()
    assert calls == []


def test_valid_signing_key_is_structurally_pass(monkeypatch, tmp_path):
    key_path = _install_signing_key(monkeypatch, tmp_path, _private_pem())
    before = key_path.read_bytes()

    (check,) = PublicationDiagnosticProvider().checks(AUTHOR)

    assert check.code == "SIGNING_KEY"
    assert check.status == "PASS"
    assert key_path.read_bytes() == before


def test_a_different_but_valid_signing_key_is_still_structurally_pass(monkeypatch, tmp_path):
    # LOCAL mode never speculates about remote trust correspondence: a key
    # that differs from whatever a Node pins is still a valid signing key.
    _install_signing_key(monkeypatch, tmp_path, _private_pem(Ed25519PrivateKey.generate()))
    trust_calls = []
    monkeypatch.setattr(
        "linuxlab.sync.trust.load_package_trusted_public_key", lambda: trust_calls.append(1)
    )

    (check,) = PublicationDiagnosticProvider().checks(AUTHOR)

    assert check.status == "PASS"
    assert trust_calls == []


@pytest.mark.parametrize(
    "pem_bytes",
    [
        b"",
        b"SIGNING_PEM_SENTINEL not a pem",
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        ),
    ],
)
def test_malformed_signing_key_fails_with_fixed_safe_evidence(monkeypatch, tmp_path, pem_bytes):
    key_path = _install_signing_key(monkeypatch, tmp_path, pem_bytes)

    (check,) = PublicationDiagnosticProvider().checks(AUTHOR)

    assert check.status == "FAIL"
    assert check.details["error_code"] == "SIGNING_KEY_INVALID"
    rendered = _rendered([check])
    assert "SIGNING_PEM_SENTINEL" not in rendered
    assert str(key_path) not in rendered
    assert str(tmp_path) not in rendered


def test_missing_signing_key_fails_safely(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "linuxlab.publication.signing._SIGNING_KEY_PATH", tmp_path / "does-not-exist.key"
    )

    (check,) = PublicationDiagnosticProvider().checks(AUTHOR)

    assert check.status == "FAIL"
    assert check.details["error_code"] == "SIGNING_KEY_INVALID"
    assert str(tmp_path) not in _rendered([check])


def test_private_key_material_never_appears_in_output(monkeypatch, tmp_path):
    pem = _private_pem()
    _install_signing_key(monkeypatch, tmp_path, pem)
    body = pem.decode().splitlines()[1]  # base64 private material line

    (check,) = PublicationDiagnosticProvider().checks(AUTHOR)

    from linuxlab.core.diagnostics import aggregate_diagnostics

    rendered = _rendered([check]) + report_to_json(aggregate_diagnostics(AUTHOR, [check]))
    assert body not in rendered
    assert "PRIVATE KEY" not in rendered


def test_unexpected_loader_failure_is_not_caught_by_the_provider(monkeypatch):
    # Only the owned typed error is translated; anything else is the
    # orchestrator's containment responsibility.
    def _boom():
        raise RuntimeError("UNEXPECTED_SENTINEL")

    monkeypatch.setattr("linuxlab.publication.diagnostics.load_package_signing_private_key", _boom)
    with pytest.raises(RuntimeError):
        PublicationDiagnosticProvider().checks(AUTHOR)
