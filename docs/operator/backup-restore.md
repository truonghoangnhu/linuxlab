# Backup and restore

This documents a tested recovery procedure, not a backup service. There is
no backup daemon, scheduler, or retention engine in LinuxLab — operators
run these steps themselves, on their own schedule.

Database dumps and secrets/keys are backed up **separately**. Never bundle
a database dump and secret material (signing key, `.env` files) into one
artifact.

## Database dump

The Task 15 release gate's own backup/restore smoke (
`scripts/release_gate/database.py`) uses `pg_dump`'s custom archive format
against the target database, with the database password passed via the
`PGPASSWORD` environment variable rather than on the command line:

```
PGPASSWORD='<postgres-password>' pg_dump \
  -h <postgres-host> -p <postgres-port> -U <postgres-user> \
  -Fc -f central-backup.dump <postgres-db>
```

Substitute the Node's own host/port/user/database for a Node backup. Never
embed the password directly in the command line or in shell history —
export it into a variable first, or supply it through a `.pgpass` file,
and confirm the dump file is non-empty afterward.

## Restore

Restore into a **fresh** database, never directly over a live one, so a
bad dump cannot corrupt the last-known-good database:

```
createdb -h <postgres-host> -p <postgres-port> -U <postgres-user> <restore-db>
PGPASSWORD='<postgres-password>' pg_restore \
  -h <postgres-host> -p <postgres-port> -U <postgres-user> \
  --no-owner -d <restore-db> central-backup.dump
```

`--no-owner` avoids a failed restore over role names that may not exist on
the target host.

## Secrets and signing keys

Back these up independently of the database dump, with the same
restrictive permissions (`0600`) they carry in production:

- `/etc/linuxlab/central.env` / `/etc/linuxlab/node.env`
- `/etc/linuxlab/keys/package-signing.key` (Central only)

Never commit any of these to Git, and never place them in the same backup
artifact as a database dump.

## Verification after restore (do not skip)

A successful `pg_restore` exit code is **not** sufficient proof of a good
restore. Verify application-level state before declaring recovery
complete:

**Central**, after restoring and pointing a Central instance at the
restored database:

- the published content snapshot(s) expected to exist are present
  (`PublicationSnapshot`);
- package metadata built from those snapshots is present;
- the expected `NodeRegistration` rows are present and `enabled`;
- durable Inbox state is intact — a previously-received event still has
  exactly one `InboxMessage` row (proves idempotency state survived, not
  just that some rows exist).

**Node**, after restoring and pointing a Node instance at the restored
database:

- the active `InstalledPackage` is present and still `state == "active"`;
- learner runtime state needed by Phase 1 (attempts/results/progress) is
  present;
- any Outbox event that was still `pending` before the backup is still
  `pending` after restore (not silently lost);
- that pending event can actually synchronize once both sides are running
  again — trigger one flush cycle and confirm it moves from `pending` to
  `delivered`:

  ```
  DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py sync_events --once
  ```

  and confirm the corresponding `InboxMessage` now exists on the restored
  Central.

Finally, confirm both restored instances report ready:

```
curl -f https://<central-host>/readyz
curl -f http://<node-host>:8080/readyz
```

This exact procedure (dump both databases, restore into fresh databases,
verify each of the checks above, then verify sync resumes) is what the
Phase 1 release gate exercises automatically as its backup/restore smoke
test — see [phase1-release-checklist.md](phase1-release-checklist.md).
