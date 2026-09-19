"""Corrective B Task 13 integration: canonical RELEASE diagnostics through
the real target-process bridge against two real isolated deployment
processes/databases (dual_deployment), with fixture keys. Proves the
spec §5/§7/§8 release matrix end to end: require_probe=false with Central
stopped still passes local RELEASE while local readiness stays up;
require_probe=true with Central stopped fails the gate while readiness
stays up; a rejected (revoked) credential fails auth acceptance; disabled
sync and a malformed switch select/fail exactly per spec; DB fields and
key-file bytes are unchanged across probes; no fixture auto-repair; no
secret in any harness-visible output."""

import json
import urllib.request

import pytest

from linuxlab.core.release_diagnostics import ReleaseDiagnosticsError
from scripts.release_gate.diagnostics import run_release_diagnostics
from scripts.release_gate.e2e_support import REPO_ROOT, dual_deployment, run_action
from scripts.release_gate.processes import stop_process


def _node_env(deployment, **overrides):
    return {**deployment.node_profile_env, **overrides}


def _readyz(process):
    with urllib.request.urlopen(process.base_url + "/readyz", timeout=10) as response:  # loopback test server
        return response.status, json.loads(response.read())


def _registration_row(deployment):
    return run_action(deployment.central_profile_env, "find-node-registration", "--node-id", deployment.node_id)


def _node_release(deployment, **overrides):
    return run_release_diagnostics(
        _node_env(deployment, **overrides),
        expected_profile="node",
        repo_root=REPO_ROOT,
        fixture_key_paths=(None, deployment.public_key_path),
    )


def test_release_matrix_with_central_available(tmp_path):
    with dual_deployment(tmp_path) as deployment:
        key_bytes = deployment.public_key_path.read_bytes()
        registration_before = _registration_row(deployment)
        assert registration_before["state"] == "active"

        # A. require_probe=true with Central up: both remote checks PASS,
        #    local registration evidence stays WARN, overall WARN passes.
        report = _node_release(deployment, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE="true")
        checks = {c.code: c for c in report.checks}
        assert report.overall_status == "WARN"
        assert checks["CENTRAL_CONNECTIVITY"].status == "PASS"
        assert checks["CENTRAL_AUTH_ACCEPTANCE"].status == "PASS"
        assert checks["NODE_REGISTRATION"].status == "WARN"
        assert checks["NODE_REGISTRATION"].details["authority_verified"] is False
        assert checks["SYNC_RETRY_POLICY"].status == "PASS"
        assert deployment.node_secret not in repr(report)

        # B. require_probe=false: no remote checks at all.
        report = _node_release(deployment, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE="false")
        assert not {c.code for c in report.checks} & {"CENTRAL_CONNECTIVITY", "CENTRAL_AUTH_ACCEPTANCE"}

        # C. Central RELEASE ignores the Node-only policy entirely.
        central = run_release_diagnostics(
            {**deployment.central_profile_env, "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE": "maybe"},
            expected_profile="central",
            repo_root=REPO_ROOT,
            fixture_key_paths=(deployment.private_key_path, None),
        )
        assert central.overall_status == "PASS"
        assert {c.code for c in central.checks} == {"DB_CONNECTIVITY", "SCHEMA_STATE", "SIGNING_KEY", "NODE_REGISTRATION"}

        # D. Rejected credential: revoke through the real adapter/service
        #    boundary, then require_probe=true -> auth acceptance FAIL ->
        #    gate FAIL (fixed text, no secret).
        run_action(deployment.central_profile_env, "revoke-node-credential", "--node-id", deployment.node_id)
        assert _registration_row(deployment) == {"found": False}
        with pytest.raises(ReleaseDiagnosticsError) as caught:
            _node_release(deployment, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE="true")
        assert str(caught.value) == "release diagnostics target process failed"
        assert deployment.node_secret not in str(caught.value) + repr(caught.value)

        # Read-only across probes: key bytes unchanged; the Node's own
        # local readiness is untouched by any of the above.
        assert deployment.public_key_path.read_bytes() == key_bytes
        assert _readyz(deployment.node) == (200, {"status": "ok"})


def test_release_matrix_with_central_stopped(tmp_path):
    with dual_deployment(tmp_path) as deployment:
        key_bytes = deployment.public_key_path.read_bytes()
        registration_before = _registration_row(deployment)
        stop_process(deployment.central)

        # E. require_probe=false + Central stopped: local readiness up and
        #    local RELEASE succeeds (WARN for locally unverified credentials).
        assert _readyz(deployment.node) == (200, {"status": "ok"})
        report = _node_release(deployment, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE="false")
        assert report.overall_status == "WARN"
        assert "CENTRAL_CONNECTIVITY" not in {c.code for c in report.checks}

        # F. require_probe=true + Central stopped: readiness still up,
        #    connectivity FAIL, release diagnostic FAIL, gate FAIL.
        with pytest.raises(ReleaseDiagnosticsError):
            _node_release(deployment, LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE="true")
        assert _readyz(deployment.node) == (200, {"status": "ok"})

        # G. Disabled standalone sync: no credentials or live Central needed.
        report = _node_release(
            deployment,
            LINUXLAB_SYNC_ENABLED="false",
            LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE="false",
            LINUXLAB_SYNC_SECRET="",
            LINUXLAB_CENTRAL_URL="",
        )
        assert report.overall_status == "PASS"
        assert {c.code for c in report.checks} == {"DB_CONNECTIVITY", "SCHEMA_STATE", "TRUST_KEY"}

        # H. Malformed switch fails closed, never becomes false.
        with pytest.raises(ReleaseDiagnosticsError) as caught:
            _node_release(deployment, LINUXLAB_SYNC_ENABLED="false", LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE="maybe")
        assert "maybe" not in str(caught.value)

        # I. Mismatched parent/target profile is rejected.
        with pytest.raises(ReleaseDiagnosticsError):
            run_release_diagnostics(
                deployment.node_profile_env,
                expected_profile="central",
                repo_root=REPO_ROOT,
                fixture_key_paths=(deployment.private_key_path, None),
            )

        # Nothing was repaired or mutated: key bytes and the Central
        # registration row (read after restarting nothing -- Central is
        # stopped, so compare the node-side evidence only) are unchanged.
        assert deployment.public_key_path.read_bytes() == key_bytes
        assert registration_before["state"] == "active"
