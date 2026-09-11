# Installation

## Production prerequisites

The supported Phase 1 production target is **Linux**. macOS is
development-only — a developer-workstation environment, not a
production-supported target — and a shipped release-gate invocation on
macOS fails at the precheck stage (see
[troubleshooting.md](troubleshooting.md)).

A production host needs:

- Linux
- Python 3.12 (`requires-python = ">=3.12,<3.13"` in `pyproject.toml`)
- PostgreSQL 18
- Gunicorn (installed as a Python dependency, pinned `gunicorn>=23,<24`)
- Caddy (the Phase 1 reference reverse proxy)
- systemd (the Linux reference process manager — application behavior does
  not depend on it, but the reference deployment templates target it)

Django and Wagtail versions are pinned in `pyproject.toml`:
`django>=5.2,<5.3`, `wagtail>=7.4,<7.5`.

## Application install

From the repository root, using a project-local virtual environment (never
the system Python):

```
python3.12 -m venv .venv
.venv/bin/pip install .
```

`pyproject.toml` is the authoritative declaration of Python dependencies —
do not install LinuxLab dependencies globally or with any other Python
interpreter.

## External configuration

LinuxLab reads its runtime configuration from environment variables, not
from files committed to the repository. In production these are supplied
by `/etc/linuxlab/central.env` or `/etc/linuxlab/node.env`, loaded by
systemd's `EnvironmentFile=` — see
[configuration.md](configuration.md) and
[deployment-systemd-caddy.md](deployment-systemd-caddy.md).

## Database

Create the PostgreSQL role and database for the deployment (Central or
Node) before running migrations, then apply migrations explicitly:

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py migrate
```

or, on a Node host:

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py migrate
```

Migration is always an explicit operator action — see
[central.md](central.md) / [node.md](node.md) for the full deployment
sequence and why it is never run automatically at service startup.

## Next steps

- Central operator: [central.md](central.md)
- Node operator: [node.md](node.md)
- Deploying with systemd/Caddy: [deployment-systemd-caddy.md](deployment-systemd-caddy.md)
