# Configuration

LinuxLab is configured entirely through environment variables. Production
values live outside the repository, in `/etc/linuxlab/central.env` or
`/etc/linuxlab/node.env` (mode `0600`, owned by the service account),
loaded by systemd via `EnvironmentFile=`. Never commit a filled-in `.env`
file to Git.

Reference templates: `deploy/env/central.env.example`,
`deploy/env/node.env.example`.

Each setting below is classified as:

- **COMMON** — read on both Central and Node
- **CENTRAL-ONLY** — only meaningful on a Central deployment
- **NODE-ONLY** — only meaningful on a Node deployment
- **SECRET** — never log, echo, or place in release evidence
- **OPTIONAL** — safe default applies if left unset

## Deployment profile

The active profile (`central` / `node` / `test`) is **not** an environment
variable. It is selected by which settings module `DJANGO_SETTINGS_MODULE`
points to:

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.central
DJANGO_SETTINGS_MODULE=linuxlab.settings.node
```

Each settings module hardcodes its own profile identity and capability
flags (author/import-package/serve-learner-content/receive-sync/run-sync-
worker) — these capabilities are not independently configurable through
environment variables.

## Core Django settings (COMMON)

| Variable | Default | Notes |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | `django-insecure-local-development-only` | **SECRET.** Generate a unique random value per deployment; restart required after a change. |
| `DJANGO_DEBUG` | `true` | Must be `false` in production. Restart required after a change. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated hostnames. Must include the real Central/Node public hostname. Restart required after a change. |
| `WAGTAILADMIN_BASE_URL` | `http://localhost:8000` | Must match the public-facing scheme/host learners and editors actually use (the Caddy listener, not the loopback Gunicorn address). Restart required after a change. |

## Database (COMMON)

| Variable | Default | Notes |
| --- | --- | --- |
| `POSTGRES_DB` | profile default (`linuxlab_central_dev` / `linuxlab_node_dev`) | Set explicitly in production; Central and Node must use separate databases and never replicate one into the other. |
| `POSTGRES_USER` | `linuxlab` | |
| `POSTGRES_PASSWORD` | *(empty)* | **SECRET.** Never appears in logs, release evidence, or shell history longer than necessary. |
| `POSTGRES_HOST` | `127.0.0.1` | |
| `POSTGRES_PORT` | `5432` | |

Restart required after any database connection setting change.

## Sync settings (read on both profiles; meaningful only where a Node runs the sync worker or Central receives events)

| Variable | Default | Notes |
| --- | --- | --- |
| `LINUXLAB_SYNC_ENABLED` | `false` | NODE-ONLY in effect. A sync-disabled Node runs fully locally. |
| `LINUXLAB_NODE_ID` | *(unset)* | NODE-ONLY in effect. Issued by Central during provisioning (see [sync-operations.md](sync-operations.md)) — never chosen locally. |
| `LINUXLAB_SYNC_SECRET` | *(unset)* | **SECRET.** NODE-ONLY in effect. Issued by Central during provisioning. Central stores only a password hash of this value, never the plaintext. |
| `LINUXLAB_CENTRAL_URL` | *(unset)* | NODE-ONLY in effect. |
| `LINUXLAB_SYNC_ALLOW_INSECURE_HTTP` | `false` | NODE-ONLY. Leave `false` in production; `LINUXLAB_CENTRAL_URL` must be HTTPS unless explicitly overridden for local development. |
| `LINUXLAB_SYNC_BATCH_MAX_EVENTS` | `100` | OPTIONAL. NODE-ONLY in effect (also enforced by Central on receipt). |
| `LINUXLAB_SYNC_BATCH_MAX_BYTES` | `1000000` | OPTIONAL. NODE-ONLY in effect (also enforced by Central on receipt). |
| `LINUXLAB_SYNC_LEASE_SECONDS` | `300` | OPTIONAL. NODE-ONLY in effect. |
| `LINUXLAB_SYNC_BACKOFF_BASE_SECONDS` | `5` | OPTIONAL. NODE-ONLY in effect. Governs ordinary transient-failure retry timing (exponential backoff with jitter) in the sync worker. |
| `LINUXLAB_SYNC_BACKOFF_CAP_SECONDS` | `3600` | OPTIONAL. NODE-ONLY in effect. Upper bound for the same backoff calculation; must be strictly greater than the base value. |

A sync-enabled Node with missing or malformed sync configuration fails
loudly (the `sync_events` management command refuses to start) rather than
silently running unsynced.

## Node classroom hostname placeholder

`deploy/env/node.env.example` uses `node.example.invalid` as a placeholder
(the `.invalid` TLD is reserved for examples and never resolves). Replace
it with the Node's real classroom DNS name or LAN IP consistently in
**both** settings, since they must describe the same public endpoint:

```
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,10.10.20.5
WAGTAILADMIN_BASE_URL=http://10.10.20.5:8080
```

`WAGTAILADMIN_BASE_URL` must name the classroom-facing endpoint learners
actually connect to (the Node Caddy listener, e.g. `:8080`), not the
loopback Gunicorn address, and its host must appear in
`DJANGO_ALLOWED_HOSTS`.

## Package signing key

Central's `.llpkg` package-signing private key is not a Django setting. It
is supplied out of band, outside the repository and outside the database
(reference location: `/etc/linuxlab/keys/package-signing.key`, mode
`0600`). It is never referenced by `central.env` and never appears in
release evidence or logs.
