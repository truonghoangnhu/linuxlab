# Deployment: systemd and Caddy

## Reference topology

```
Client
  |
Caddy (reverse proxy, TLS termination for Central)
  |  localhost
Gunicorn
  |
Django/Wagtail
  |
PostgreSQL
```

Gunicorn is never exposed directly to the Internet or classroom LAN in the
reference deployment; Caddy always sits in front of it. Central production
traffic is HTTPS. Node classroom traffic may be plain HTTP or HTTPS
depending on local infrastructure, but a reverse proxy still sits in front
of Gunicorn either way.

Central and Node are the same codebase running as two independent
deployment profiles (`linuxlab.settings.central` / `linuxlab.settings.node`)
with separate processes and separate PostgreSQL databases — never a
shared database, and never automatic replication between them.

## Versioned reference templates

All files below are version-controlled in this repository under `deploy/`
and contain no real secrets — only explicit placeholders. They are a
starting point to copy and adjust per host, not a mechanism this project
installs automatically.

```
deploy/
├── systemd/
│   ├── linuxlab-central.service
│   └── linuxlab-node.service
├── caddy/
│   ├── Caddyfile.central.example
│   └── Caddyfile.node.example
└── env/
    ├── central.env.example
    └── node.env.example
```

### systemd units

`deploy/systemd/linuxlab-central.service` and
`deploy/systemd/linuxlab-node.service` both run Gunicorn bound to a local
interface only:

```
ExecStart=/opt/linuxlab/.venv/bin/gunicorn --bind 127.0.0.1:8000 --workers 3 linuxlab.wsgi:application
```

Configuration is loaded from an external environment file, never embedded
in the unit:

```
EnvironmentFile=/etc/linuxlab/central.env   # Central
EnvironmentFile=/etc/linuxlab/node.env      # Node
```

Neither unit runs a migration step (`ExecStartPre=` or otherwise) —
migration is always an explicit, separate operator action; see
[central.md](central.md) and [node.md](node.md). The Node unit only runs
Gunicorn; the Node → Central sync worker (`manage.py sync_events`) is a
separate, independently supervised process — see
[sync-operations.md](sync-operations.md).

Install (adjust `User`/`Group`/`WorkingDirectory`/paths for the target
host first):

```
sudo cp deploy/systemd/linuxlab-central.service /etc/systemd/system/linuxlab-central.service
sudo systemctl daemon-reload
sudo systemctl enable --now linuxlab-central
```

Substitute `linuxlab-node.service` for a Node host.

### Caddy

`deploy/caddy/Caddyfile.central.example` reverse-proxies a real public
hostname to loopback Gunicorn, which gives Caddy automatic HTTPS:

```
central.example.org {
	reverse_proxy 127.0.0.1:8000
}
```

Replace `central.example.org` with the real public hostname before use.

`deploy/caddy/Caddyfile.node.example` uses a bare port for a classroom LAN
with no local DNS/certificate authority (plain HTTP, no automatic TLS):

```
:8080 {
	reverse_proxy 127.0.0.1:8000
}
```

Replace `:8080` with a real hostname instead to get Caddy's automatic
HTTPS on the classroom LAN, matching the Central example.

### Environment files

`deploy/env/central.env.example` and `deploy/env/node.env.example` are
templates only. On the target host, always create the destination file
before editing it — running the `install` step again after editing would
truncate what was just written:

```
sudo install -m 0600 -o linuxlab -g linuxlab /dev/null /etc/linuxlab/central.env
sudoedit /etc/linuxlab/central.env
```

See [configuration.md](configuration.md) for the meaning of every setting.
Never commit the filled-in file to Git.

## What this repository does not provide

There is no automatic Linux installer/provisioner, no Docker/Docker
Compose requirement, and no parallel deployment mechanism — the templates
above are the only supported reference path. CI performs static/contract
checks against these exact files (`scripts/tests/test_deploy_templates.py`);
do not introduce a second, undocumented deployment path that diverges from
them.
