# Node operations

A Node is a local classroom deployment: it runs `linuxlab.settings.node`,
serves learner content from installed packages, records learner
attempts/progress locally, and (when enabled) synchronizes completed
events to Central.

## Readiness

```
GET /healthz    # process liveness only -- no DB query
GET /readyz     # DB connectivity, migration state, profile/config validity,
                 # and (if sync is enabled) sync configuration validity
```

A sync-enabled Node with missing or malformed sync configuration reports
`not_ready` rather than silently running unsynced.

## Package activation and runtime

Node verifies, stages, activates, and rolls back `.llpkg` packages through
internal service functions
(`linuxlab.sync.services.import_package` / `activate_package` /
`rollback_package` / `get_active_package`). A verified package is staged
first; activation is atomic and archives whatever package was previously
active in the same scope; rollback returns to the previously-active
package without re-verifying it.

**Known Phase 1 limitation:** as with Central's package build/sign step
(see [central.md](central.md)), there is currently **no shipped operator
CLI, admin action, or API endpoint** to import or activate a package on a
production Node. Only the Task 15 release-gate test harness exercises this
path today, to validate the release gate itself. Getting a package onto a
production Node during Phase 1 requires engineering support; this gap is
tracked as a concern for Phase 1.5.

Once a package is active, learners reach it through the normal learner
routes (`/learn/<source>/`, `/learn/<source>/activity/<id>/<version>/`,
etc.) with no operator action required per lesson.

## Local (offline) operation

A Node with `LINUXLAB_SYNC_ENABLED=false` (the default) runs fully
locally: learners can start, attempt, and complete activities with no
Internet connectivity and no dependency on Central being reachable. No
`OutboxMessage` rows are created while sync is disabled, and there is no
automatic backfill of pre-enrollment activity if sync is enabled later.

## Sync worker

The Node → Central sync worker is a **separate process** from the web
process — it never runs as a background thread inside Gunicorn:

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py sync_events --once   # one flush cycle
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py sync_events           # continuous loop
```

No reference systemd unit ships for this worker (see
[deployment-systemd-caddy.md](deployment-systemd-caddy.md)) — supervise it
with your own service definition. Full lifecycle, credential handling, and
troubleshooting: [sync-operations.md](sync-operations.md).

## Safe restart

```
backup (see backup-restore.md)
  -> deploy new code
  -> DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py migrate
  -> DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py check
  -> sudo systemctl restart linuxlab-node
  -> curl -f http://<node-host>:8080/readyz
```

Migration is always an explicit step, never automatic at service startup.
An active installed package and any pending sync Outbox survive a restart
— restart does not require re-importing packages or re-provisioning sync.

```
sudo systemctl status linuxlab-node
sudo systemctl restart linuxlab-node
sudo journalctl -u linuxlab-node -n 200 --no-pager
```

## Classroom LAN hostname

Replace the `node.example.invalid` placeholder in `node.env` with the
Node's real classroom DNS name or LAN IP — see
[configuration.md](configuration.md) for the exact settings to update and
why they must stay consistent with each other.
