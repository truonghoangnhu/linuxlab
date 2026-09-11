"""Task 15 Task 12: versioned deployment templates and contract tests
(spec section 20, "Deployment Artifacts").

Pure-Python, filesystem-only, database-free: these check structural/
contractual properties of the reference systemd/Caddy/env templates
under `deploy/` -- never exact formatting or comment wording.
"""

import re
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = REPO_ROOT / "deploy"

CENTRAL_SERVICE = DEPLOY_ROOT / "systemd" / "linuxlab-central.service"
NODE_SERVICE = DEPLOY_ROOT / "systemd" / "linuxlab-node.service"
CENTRAL_CADDYFILE = DEPLOY_ROOT / "caddy" / "Caddyfile.central.example"
NODE_CADDYFILE = DEPLOY_ROOT / "caddy" / "Caddyfile.node.example"
CENTRAL_ENV = DEPLOY_ROOT / "env" / "central.env.example"
NODE_ENV = DEPLOY_ROOT / "env" / "node.env.example"

ALL_TEMPLATE_FILES = [
    CENTRAL_SERVICE,
    NODE_SERVICE,
    CENTRAL_CADDYFILE,
    NODE_CADDYFILE,
    CENTRAL_ENV,
    NODE_ENV,
]


# --- existence ----------------------------------------------------------


def test_expected_template_files_exist():
    for path in ALL_TEMPLATE_FILES:
        assert path.is_file(), f"missing required deployment template: {path}"


# --- systemd --------------------------------------------------------------


def _bind_port(service_text: str) -> str:
    match = re.search(r"--bind\s+(\S+)", service_text)
    assert match, "ExecStart must bind gunicorn to an explicit address"
    return match.group(1)


def test_systemd_units_use_gunicorn_with_wsgi_app():
    for path in (CENTRAL_SERVICE, NODE_SERVICE):
        text = path.read_text()
        exec_start = re.search(r"^ExecStart=.*$", text, re.MULTILINE)
        assert exec_start, f"{path} must declare ExecStart="
        assert "gunicorn" in exec_start.group(0)
        assert "linuxlab.wsgi:application" in exec_start.group(0)


def test_systemd_units_bind_local_interface_only():
    for path in (CENTRAL_SERVICE, NODE_SERVICE):
        bind_addr = _bind_port(path.read_text())
        host = bind_addr.rsplit(":", 1)[0]
        assert host in ("127.0.0.1", "localhost"), (
            f"{path} must bind Gunicorn to a local interface, not {host!r}"
        )


def test_systemd_units_load_config_from_external_environment_file():
    assert "EnvironmentFile=/etc/linuxlab/central.env" in CENTRAL_SERVICE.read_text()
    assert "EnvironmentFile=/etc/linuxlab/node.env" in NODE_SERVICE.read_text()


def test_systemd_units_never_embed_secrets():
    forbidden = ("PASSWORD", "SECRET", "PRIVATE_KEY")
    for path in (CENTRAL_SERVICE, NODE_SERVICE):
        text = path.read_text().upper()
        for token in forbidden:
            assert token not in text, f"{path} must not reference {token!r} directly"


def test_systemd_units_never_run_migrations():
    for path in (CENTRAL_SERVICE, NODE_SERVICE):
        for line in path.read_text().splitlines():
            stripped = line.strip().lower()
            assert not stripped.startswith("execstartpre"), (
                f"{path} must not run migrations automatically via ExecStartPre"
            )
            assert "manage.py migrate" not in stripped


# --- env examples -----------------------------------------------------


def _parse_env(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_env_examples_declare_correct_settings_profile():
    assert _parse_env(CENTRAL_ENV)["DJANGO_SETTINGS_MODULE"] == "linuxlab.settings.central"
    assert _parse_env(NODE_ENV)["DJANGO_SETTINGS_MODULE"] == "linuxlab.settings.node"


def test_env_examples_use_placeholders_not_real_secrets():
    secret_keys = ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD")
    for path in (CENTRAL_ENV, NODE_ENV):
        values = _parse_env(path)
        for key in secret_keys:
            assert key in values, f"{path} must document {key}"
            assert values[key].lower().startswith("changeme"), (
                f"{path}:{key} must be an explicit placeholder, not a real value"
            )

    node_values = _parse_env(NODE_ENV)
    assert node_values["LINUXLAB_SYNC_SECRET"].lower().startswith("changeme")


def test_env_examples_debug_disabled_for_production_reference():
    for path in (CENTRAL_ENV, NODE_ENV):
        assert _parse_env(path)["DJANGO_DEBUG"].lower() == "false"


def test_node_env_declares_sync_identity_fields():
    node_values = _parse_env(NODE_ENV)
    for key in ("LINUXLAB_NODE_ID", "LINUXLAB_CENTRAL_URL", "LINUXLAB_SYNC_ENABLED"):
        assert key in node_values, f"node.env.example must document {key}"


# --- Caddy --------------------------------------------------------------


def test_caddyfiles_reverse_proxy_matches_gunicorn_bind_port():
    central_port = _bind_port(CENTRAL_SERVICE.read_text()).rsplit(":", 1)[1]
    node_port = _bind_port(NODE_SERVICE.read_text()).rsplit(":", 1)[1]

    central_caddy = CENTRAL_CADDYFILE.read_text()
    node_caddy = NODE_CADDYFILE.read_text()

    assert re.search(rf"reverse_proxy\s+127\.0\.0\.1:{central_port}\b", central_caddy)
    assert re.search(rf"reverse_proxy\s+127\.0\.0\.1:{node_port}\b", node_caddy)


def test_central_caddyfile_represents_https_requirement():
    first_directive_line = next(
        line.strip()
        for line in CENTRAL_CADDYFILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert not first_directive_line.startswith(":"), (
        "Central's site address must be a hostname (not a bare port) so Caddy "
        "provisions automatic HTTPS, matching 'Central production traffic is HTTPS'"
    )
    assert not first_directive_line.startswith("http://")


# --- Node classroom-LAN host contract -----------------------------------
#
# Node Caddy terminates classroom-LAN HTTP on :8080 and proxies to loopback
# Gunicorn (Task 12 corrective, independent review B). The Node env example
# must describe the classroom-facing endpoint learners actually connect to
# -- not just the Gunicorn loopback address -- or WAGTAILADMIN_BASE_URL
# misrepresents the real endpoint and, more importantly, a real classroom
# Host header would be rejected by Django's ALLOWED_HOSTS.

LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def _node_caddy_site_address() -> str:
    first_line = next(
        line.strip()
        for line in NODE_CADDYFILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    return first_line.split("{")[0].strip()


def test_node_wagtailadmin_base_url_is_not_loopback_only():
    parsed = urlparse(_parse_env(NODE_ENV)["WAGTAILADMIN_BASE_URL"])
    assert parsed.hostname not in LOOPBACK_HOSTNAMES, (
        "Node WAGTAILADMIN_BASE_URL must describe the classroom-facing "
        "endpoint learners actually connect to, not the Gunicorn loopback address"
    )


def test_node_wagtailadmin_base_url_host_is_in_allowed_hosts():
    node_values = _parse_env(NODE_ENV)
    parsed = urlparse(node_values["WAGTAILADMIN_BASE_URL"])
    allowed_hosts = node_values["DJANGO_ALLOWED_HOSTS"].split(",")

    # A loopback-only WAGTAILADMIN_BASE_URL cannot describe the real
    # classroom endpoint, so the ALLOWED_HOSTS correspondence below is only
    # meaningful once it points at the actual classroom-facing host.
    assert parsed.hostname not in LOOPBACK_HOSTNAMES, (
        "WAGTAILADMIN_BASE_URL is localhost-only and does not describe the "
        "classroom-facing endpoint -- fix that before this check is meaningful"
    )
    assert parsed.hostname in allowed_hosts, (
        "the classroom-facing host in WAGTAILADMIN_BASE_URL must be present "
        "in DJANGO_ALLOWED_HOSTS or Django rejects the request with HTTP 400"
    )


def test_node_wagtailadmin_base_url_port_matches_caddy_classroom_listener():
    parsed = urlparse(_parse_env(NODE_ENV)["WAGTAILADMIN_BASE_URL"])

    caddy_address = _node_caddy_site_address()
    assert caddy_address.startswith(":"), (
        "expected a bare-port Node Caddy site address (classroom-LAN HTTP); "
        "this test derives the expected port from it rather than hardcoding one"
    )
    caddy_port = int(caddy_address[1:])

    assert parsed.port == caddy_port, (
        "WAGTAILADMIN_BASE_URL's port must match the port Node Caddy actually "
        "listens on for classroom traffic"
    )


def test_node_wagtailadmin_base_url_scheme_matches_caddy_example():
    parsed = urlparse(_parse_env(NODE_ENV)["WAGTAILADMIN_BASE_URL"])

    caddy_address = _node_caddy_site_address()
    if caddy_address.startswith(":"):
        # A bare ":port" Caddy address gets no automatic TLS -- plain HTTP,
        # matching the classroom-LAN-HTTP topology. HTTPS is not required.
        assert parsed.scheme == "http"
    else:
        assert parsed.scheme == "https"


def test_node_allowed_hosts_keeps_loopback_and_adds_classroom_host():
    allowed_hosts = _parse_env(NODE_ENV)["DJANGO_ALLOWED_HOSTS"].split(",")

    assert "localhost" in allowed_hosts
    assert "127.0.0.1" in allowed_hosts

    non_loopback = [host for host in allowed_hosts if host not in LOOPBACK_HOSTNAMES]
    assert non_loopback, (
        "Node DJANGO_ALLOWED_HOSTS must also include a classroom-facing host "
        "placeholder, or real classroom LAN requests fail ALLOWED_HOSTS"
    )


# --- env install/edit instruction hazard --------------------------------
#
# The example instructions must never tell an operator to fill in the
# destination file and then run `install ... /dev/null <destination>`,
# which would truncate whatever the operator just edited. The destination
# must be created first, then edited in place.


def _line_index(lines, *required_substrings):
    for index, line in enumerate(lines):
        if all(substring in line for substring in required_substrings):
            return index
    return None


def test_node_env_example_creates_destination_before_editing_it():
    lines = NODE_ENV.read_text().splitlines()
    install_index = _line_index(lines, "install", "/etc/linuxlab/node.env")
    edit_index = _line_index(lines, "sudoedit", "/etc/linuxlab/node.env")

    assert install_index is not None, (
        "node.env.example must document creating /etc/linuxlab/node.env "
        "(e.g. via `install`) before it is edited"
    )
    assert edit_index is not None, (
        "node.env.example must document editing the destination file "
        "in place (e.g. via `sudoedit`), not editing this template itself"
    )
    assert install_index < edit_index, (
        "creating the destination file must come before editing it, or "
        "following the documented order would truncate operator edits"
    )


def test_central_env_example_creates_destination_before_editing_it():
    lines = CENTRAL_ENV.read_text().splitlines()
    install_index = _line_index(lines, "install", "/etc/linuxlab/central.env")
    edit_index = _line_index(lines, "sudoedit", "/etc/linuxlab/central.env")

    assert install_index is not None, (
        "central.env.example must document creating /etc/linuxlab/central.env "
        "(e.g. via `install`) before it is edited"
    )
    assert edit_index is not None, (
        "central.env.example must document editing the destination file "
        "in place (e.g. via `sudoedit`), not editing this template itself"
    )
    assert install_index < edit_index, (
        "creating the destination file must come before editing it, or "
        "following the documented order would truncate operator edits"
    )


# --- architecture invariants -------------------------------------------


def test_deploy_templates_never_reference_docker_or_kubernetes():
    forbidden = ("docker", "kubernetes", "kubectl", "k8s")
    for path in ALL_TEMPLATE_FILES:
        text = path.read_text().lower()
        for token in forbidden:
            assert token not in text, f"{path} must not reference {token!r}"


def test_deploy_templates_never_reference_macos_production_mechanisms():
    forbidden = ("launchd", "launchctl", "homebrew", "brew install", ".app/contents")
    for path in ALL_TEMPLATE_FILES:
        text = path.read_text().lower()
        for token in forbidden:
            assert token not in text, f"{path} must not reference macOS mechanism {token!r}"
