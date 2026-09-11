# Central operations

Central is the authoring/publishing side of LinuxLab: it runs
`linuxlab.settings.central`, authors content in Wagtail, receives synced
learner events from Nodes, and is the trust root for `.llpkg` package
signing.

## Readiness

```
GET /healthz    # process liveness only -- no DB query, returns quickly
GET /readyz     # DB connectivity, migration state, profile/config validity
```

`/readyz` returns `{"status":"ok"}` (HTTP 200) when ready, or
`{"status":"not_ready"}` (HTTP 503) otherwise. It never runs migrations or
mutates state, and the response body deliberately carries no diagnostic
detail — check the sanitized application log for the reason (see
[troubleshooting.md](troubleshooting.md)).

## Content publication (Wagtail editorial workflow)

Editors author and revise `LearningPathPage` / `ModulePage` / `LessonPage`
content through the normal Wagtail admin at `/admin/`. This is Wagtail's
own page-revision workflow and is editorial state, not a LinuxLab
published version — a Wagtail "published" page revision is not yet an
immutable `PublicationSnapshot`.

**Known Phase 1 limitation:** producing an immutable `PublicationSnapshot`
from authored content (`linuxlab.publication.snapshots.publish_learning_path`)
and building a signed `.llpkg` package from it
(`linuxlab.publication.package.build_package`) are implemented as internal
service functions with **no shipped operator CLI, admin action, or API
endpoint** as of Phase 1. The only code path that currently exercises them
is the Task 15 release-gate test harness (`scripts/release_gate/`), which
exists to validate the release gate, not as an operator tool. Publishing
content and distributing packages to Nodes during Phase 1 therefore
requires engineering support; this gap is tracked as a concern for
Phase 1.5.

## Node registration lifecycle

Central issues, rotates, and revokes each Node's sync bearer credential
through a management command (Central profile only):

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py manage_sync_node issue --name "Lab A"
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py manage_sync_node rotate <node_uuid> --grace-hours 24
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py manage_sync_node revoke <node_uuid>
```

`issue` and `rotate` print the Node UUID and the plaintext bearer secret
**exactly once** — capture it immediately and transfer it to the Node's
`central.env` out of band. Central stores only a password hash of the
secret and cannot display it again. `rotate` keeps the previous secret
valid for the given grace period so an in-flight Node does not lose sync
mid-rotation; `revoke` disables both the current and previous secret
immediately.

See [sync-operations.md](sync-operations.md) for the full sync lifecycle,
including how a revoked/rotated credential shows up operationally on the
Node side.

## Safe restart and recovery

```
backup (see backup-restore.md)
  -> deploy new code
  -> DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py migrate
  -> DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py check
  -> sudo systemctl restart linuxlab-central
  -> curl -f https://<central-host>/readyz
```

Migration is always an explicit step run before restarting Gunicorn —
never automatic at service startup (`deploy/systemd/linuxlab-central.service`
deliberately has no `ExecStartPre` migration line). A restart is not safe
to consider complete until `/readyz` confirms `{"status":"ok"}`.

```
sudo systemctl status linuxlab-central
sudo systemctl restart linuxlab-central
sudo journalctl -u linuxlab-central -n 200 --no-pager
```

## HTTPS

Central production traffic is HTTPS, terminated by Caddy in front of
loopback Gunicorn — see
[deployment-systemd-caddy.md](deployment-systemd-caddy.md).
