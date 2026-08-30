"""WSGI entrypoint for LinuxLab (T15-D12: Gunicorn is the Phase 1
production reference server for both Central and Node).

The deployment profile is selected by DJANGO_SETTINGS_MODULE, set
explicitly in the process environment before this module is imported
(by systemd's EnvironmentFile= in production, or by the release gate's
child process environment) -- the `central` default here only matches
manage.py's own fallback and is never relied on by a real deployment.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linuxlab.settings.central")

application = get_wsgi_application()
