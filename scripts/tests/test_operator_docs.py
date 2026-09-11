"""Task 15 Task 13: modular operator runbooks (spec section 21,
"Operator Documentation").

Pure-Python, filesystem-only, database-free: these check structural/
contractual properties of the operator documentation under
`docs/operator/` -- never exact prose wording, paragraph order, or
formatting.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATOR_ROOT = REPO_ROOT / "docs" / "operator"

REQUIRED_FILES = [
    "README.md",
    "installation.md",
    "central.md",
    "node.md",
    "configuration.md",
    "deployment-systemd-caddy.md",
    "backup-restore.md",
    "sync-operations.md",
    "troubleshooting.md",
    "phase1-release-checklist.md",
]

# The only release-gate CLI flag that actually exists
# (`scripts/phase1_release_gate.py --help`).
REAL_RELEASE_GATE_FLAGS = {"--keep-on-failure", "-h", "--help"}

# Real deployment template filenames (Task 12, spec section 20) that
# operator docs must reference rather than a parallel/invented path.
REAL_TEMPLATE_PATHS = [
    "deploy/systemd/linuxlab-central.service",
    "deploy/systemd/linuxlab-node.service",
    "deploy/caddy/Caddyfile.central.example",
    "deploy/caddy/Caddyfile.node.example",
    "deploy/env/central.env.example",
    "deploy/env/node.env.example",
]

# Real release-gate exit codes (spec section 18, "Failure Model").
EXIT_CODE_NAMES = [
    "PRECHECK_FAILURE",
    "STARTUP_FAILURE",
    "SCENARIO_FAILURE",
    "CLEANUP_FAILURE",
]


def _read(name: str) -> str:
    return (OPERATOR_ROOT / name).read_text()


def _all_docs_text() -> str:
    return "\n".join(_read(name) for name in REQUIRED_FILES)


# --- existence -----------------------------------------------------------


def test_required_operator_files_exist():
    for name in REQUIRED_FILES:
        path = OPERATOR_ROOT / name
        assert path.is_file(), f"missing required operator doc: {path}"


def test_no_unresolved_placeholder_markers():
    for name in REQUIRED_FILES:
        text = _read(name)
        for marker in ("TODO", "TBD", "FIXME"):
            assert marker not in text, f"{name} contains unresolved {marker!r} marker"


# --- deployment template cross-check --------------------------------------


def test_deployment_doc_references_real_template_paths():
    text = _read("deployment-systemd-caddy.md")
    for template_path in REAL_TEMPLATE_PATHS:
        assert template_path in text, (
            f"deployment-systemd-caddy.md must reference real template {template_path}"
        )


def test_operator_docs_do_not_invent_parallel_deployment_paths():
    text = _all_docs_text()
    # No operator doc may instruct adding an actual ExecStartPre=...migrate
    # systemd directive -- Task 12 deliberately keeps migration an
    # explicit, separate operator step. (Prose *describing* the templates'
    # lack of one, e.g. "no ExecStartPre= directive", is fine -- only a
    # line that pairs the directive with a migrate invocation would be a
    # real instruction to add automatic migration.)
    for line in text.splitlines():
        if line.strip().startswith("ExecStartPre="):
            assert "migrate" not in line.lower()


# --- migrations ------------------------------------------------------------


def test_migration_procedure_is_explicit_not_automatic():
    text = _read("central.md") + _read("node.md")
    assert "manage.py migrate" in text
    lowered = text.lower()
    assert "explicit" in lowered


# --- release gate CLI --------------------------------------------------


def test_release_checklist_only_references_real_release_gate_flags():
    text = _read("phase1-release-checklist.md") + _read("troubleshooting.md")
    # Scope to lines that actually reference the release-gate script --
    # other tools documented elsewhere (journalctl, manage.py, ...) have
    # their own real flags that are out of scope for this check.
    for line in text.splitlines():
        if "phase1_release_gate.py" not in line and "release-gate" not in line:
            continue
        for flag in re.findall(r"--[a-z][a-z-]*", line):
            assert flag in REAL_RELEASE_GATE_FLAGS, (
                f"operator docs reference a release-gate flag {flag!r} that does not exist"
            )


def test_release_checklist_references_real_exit_codes():
    text = _read("phase1-release-checklist.md") + _read("troubleshooting.md")
    combined_exit_code_text = text
    assert any(name in combined_exit_code_text for name in EXIT_CODE_NAMES)


# --- platform policy -----------------------------------------------------


def test_installation_states_linux_is_production_macos_is_dev_only():
    text = _read("installation.md")
    assert "Linux" in text
    lowered = text.lower()
    assert "macos" in lowered
    assert "development-only" in lowered or "development only" in lowered


def test_no_nonexistent_macos_bypass_flag_documented():
    text = _all_docs_text()
    assert "macos-bypass" not in text.lower()
    assert "--skip-platform-check" not in text
    assert "check_platform=False" not in text


# --- node placeholder hostname -------------------------------------------


def test_node_docs_mention_replacing_example_invalid_placeholder():
    text = _read("node.md") + _read("configuration.md")
    assert "node.example.invalid" in text
    lowered = text.lower()
    assert "replace" in lowered


def test_node_docs_explain_allowed_hosts_and_public_url_consistency():
    text = _read("node.md") + _read("configuration.md")
    assert "DJANGO_ALLOWED_HOSTS" in text
    assert "WAGTAILADMIN_BASE_URL" in text


# --- central HTTPS ---------------------------------------------------------


def test_central_https_requirement_is_represented():
    text = _read("central.md") + _read("deployment-systemd-caddy.md")
    assert "HTTPS" in text


# --- secrets ---------------------------------------------------------------


FORBIDDEN_SECRET_TOKENS = ("BEGIN RSA", "BEGIN OPENSSH", "BEGIN PRIVATE KEY")


def test_no_embedded_real_looking_secrets():
    text = _all_docs_text()
    for token in FORBIDDEN_SECRET_TOKENS:
        assert token not in text
    # Every POSTGRES_PASSWORD/DJANGO_SECRET_KEY/SYNC_SECRET example must be
    # an obvious placeholder, never a real-looking configured value.
    for match in re.finditer(r"^(POSTGRES_PASSWORD|DJANGO_SECRET_KEY|LINUXLAB_SYNC_SECRET)=(\S+)", text, re.MULTILINE):
        value = match.group(2).strip("`\"'")
        assert "changeme" in value.lower() or "<" in value, (
            f"{match.group(1)} example value {value!r} does not look like a placeholder"
        )


def test_sync_operations_does_not_instruct_direct_sql_mutation():
    text = _read("sync-operations.md")
    lowered = text.lower()
    assert "update outboxmessage" not in lowered
    assert "update inboxmessage" not in lowered
    assert "raw sql" not in lowered or "must not" in lowered


# --- README routes operators -------------------------------------------


def test_readme_links_every_other_operator_doc():
    text = _read("README.md")
    for name in REQUIRED_FILES:
        if name == "README.md":
            continue
        assert name in text, f"README.md must route operators to {name}"
