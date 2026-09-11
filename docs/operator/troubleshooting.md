# Troubleshooting

Diagnose in this order — each step narrows the problem before you reach
for a more invasive fix.

```
1. /healthz
2. /readyz
3. systemd service status
4. sanitized application log
5. PostgreSQL connectivity
6. migration state
7. profile/capability
8. package lifecycle
9. sync credential
10. Outbox/Inbox state
```

```
curl -f https://<central-host>/healthz
curl -f https://<central-host>/readyz
sudo systemctl status linuxlab-central
sudo journalctl -u linuxlab-central -n 200 --no-pager
```

(Substitute the Node's own host/port and `linuxlab-node` for a Node.)

`/healthz` proves only that the process is alive — it never touches the
database. `/readyz` proves database connectivity, migration-state
compatibility, valid profile, and required configuration for that profile
— but its HTTP response body only ever says `{"status":"ok"}` or
`{"status":"not_ready"}`; it never discloses which check failed. Read the
sanitized application log (`journalctl`) for the actual reason.

## `/healthz` succeeds while `/readyz` fails

The process is alive but not ready to serve traffic. Check, in order:
PostgreSQL reachable (`POSTGRES_HOST`/`PORT`/`USER`/`PASSWORD` correct and
the database running), pending migrations (`manage.py migrate` not yet
run after a deploy), and — on a sync-enabled Node — malformed sync
configuration (`LINUXLAB_NODE_ID` not a UUID, empty
`LINUXLAB_SYNC_SECRET`, or `LINUXLAB_CENTRAL_URL` missing/invalid).

## Restored database exists but service remains `not_ready`

After a restore, `manage.py migrate` must still be run against the
restored database before the service is ready if the restored dump
predates the currently-deployed code's migrations — restoring a dump does
not itself run migrations. Confirm with:

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py migrate --check
```

## Central returns 401/403

- **401** on the sync endpoint: the `Authorization`/`X-LinuxLab-Node-Id`
  headers are missing or malformed, or the credential does not match any
  known Node.
- **403**: the credential matches a known Node whose registration is
  disabled/revoked, or the deployment profile does not accept sync
  ingestion at all (a Central profile always does; a Node profile never
  does).

Reissue or rotate the Node's credential (see
[sync-operations.md](sync-operations.md)) and confirm `node.env` was
updated to match.

## Node learning works but sync does not

Local learning never depends on sync — this is expected while Central is
unreachable or the Node's credential is invalid. Run one manual flush
cycle and read its summary line:

```
DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py sync_events --once
```

A `protocol_fault` in the summary means Central rejected a batch as
malformed (HTTP 400) — check `LINUXLAB_CENTRAL_URL` and application
version compatibility. An `auth_fault` means a credential problem — see
above.

## Package import fails / signature verification fails

Package verification is deliberately fail-closed: an invalid signature
means the existing active package is left unchanged and no partial
package state is created. Confirm the `.llpkg` was actually built and
signed by Central against the trusted public key the Node expects — see
the known Phase 1 limitation noted in [central.md](central.md) and
[node.md](node.md) (there is currently no operator CLI for this step; it
requires engineering support).

## Outbox remains `PENDING`

See [sync-operations.md](sync-operations.md) "Diagnosing pending /
dead-letter state" and "HTTP 400 vs 413" — a pending event is normal
during an outage or an invalid credential; it only indicates a real
problem once Central is reachable, the credential is valid, and the
worker has actually run.

## Release gate fails

Read the exit code (`scripts/phase1_release_gate.py`, spec section 18):

```
0 = PASS
2 = PRECHECK_FAILURE   (environment/repo/dependency/db-safety/config)
3 = STARTUP_FAILURE    (db migration, Gunicorn startup, readiness)
4 = SCENARIO_FAILURE   (happy-path/negative/recovery/invariant failure)
5 = CLEANUP_FAILURE    (required cleanup cannot be completed)
```

Sanitized evidence and diagnostics are always written to
`artifacts/phase1-release/<run_id>/`, PASS or FAIL. `--keep-on-failure` is
a local-development-only debugging aid that keeps that already-sanitized
evidence bundle on a failed run — it is rejected in CI and it never
retains raw secrets or execution state. There is no macOS bypass flag: a
shipped release-gate run on macOS fails at the precheck stage by design
(Linux is the Phase 1 production/release authority; macOS is
development-only).
