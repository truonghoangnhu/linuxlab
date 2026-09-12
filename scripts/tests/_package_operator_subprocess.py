"""Test-only subprocess bootstrap for
scripts/tests/test_package_operator_e2e.py. Never imported by
production code. Runs `django.setup()` once, optionally points the
Central signing-key loader or Node trust-anchor loader at a
test-generated file (a plain module-attribute assignment, valid only
for this one short-lived process), then either bootstraps one
LearningPathPage or calls one real management command unmodified.
"""

import json
import sys
from pathlib import Path

# This script lives under scripts/tests/, not the repo root, so when it is
# invoked directly (python .../_package_operator_subprocess.py, exactly how
# the E2E test's subprocess.run() calls it), Python sets sys.path[0] to this
# file's own directory -- never the repo root, regardless of the child
# process's cwd=. `manage.py`-based subprocess helpers elsewhere (e.g.
# scripts/release_gate/database.py's run_migrations()) never need this
# because manage.py itself lives at the repo root. Insert the repo root
# explicitly so `import linuxlab` (and `import django`, whose console
# entry point is not used here) resolve regardless of invocation style.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import django

django.setup()

from django.core.management import call_command
from django.core.management.base import CommandError


def _bootstrap_learning_path(request):
    from linuxlab.content.models import LearningPathPage
    from linuxlab.publication.tests._wagtail_bootstrap import get_or_create_root_page

    root = get_or_create_root_page()
    page = LearningPathPage(title=request["title"], slug=request["slug"])
    root.add_child(instance=page)
    print(f"LEARNING_PATH_PUBLIC_ID:{page.public_id}")


def main():
    request = json.loads(sys.argv[1])

    if request.get("signing_key_path"):
        import linuxlab.publication.signing as signing_module

        signing_module._SIGNING_KEY_PATH = Path(request["signing_key_path"])

    if request.get("trust_anchor_path"):
        import linuxlab.sync.trust as trust_module

        trust_module._TRUST_ANCHOR_PATH = Path(request["trust_anchor_path"])

    if request["mode"] == "bootstrap_learning_path":
        _bootstrap_learning_path(request)
        return

    try:
        call_command(*request["command"])
    except CommandError as exc:
        print(f"COMMAND_ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
