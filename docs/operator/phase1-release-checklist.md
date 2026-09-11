# Phase 1 human release checklist

This is the human approval checklist for closing Task 15 / Phase 1
Learning Foundation. It does not automatically create a git tag — a
release candidate or final tag remains an explicit, separate human action
taken only after every item below is checked and the independent Task 15
review has returned PASS.

```
CODE
[ ] FAST CI PASS
[ ] RELEASE CI PASS
[ ] working tree/release SHA confirmed
[ ] no migration drift

SECURITY
[ ] no secrets in repo/evidence/log
[ ] package signing verified
[ ] Node credential lifecycle verified

CENTRAL/NODE
[ ] both /readyz PASS
[ ] dual-deployment E2E PASS
[ ] offline behavior PASS
[ ] retry/idempotency PASS

RECOVERY
[ ] Central restore smoke PASS
[ ] Node restore smoke PASS

OPERATIONS
[ ] deployment templates reviewed
[ ] operator docs current
[ ] known Minor backlog recorded

EVIDENCE
[ ] release-evidence.json present
[ ] release-evidence.md present
[ ] evidence git_sha == release candidate SHA
```

## Where the evidence for each item comes from

- **FAST CI PASS** — `.github/workflows/ci-fast.yml` on the release
  candidate SHA.
- **RELEASE CI PASS** — `.github/workflows/phase1-release-gate.yml` on the
  exact release candidate SHA (a PASS from a different SHA is not release
  evidence for this SHA).
- **release-gate exit code** — `scripts/phase1_release_gate.py` must exit
  `0`. See [troubleshooting.md](troubleshooting.md) for the full exit-code
  table if it does not.
- **release-evidence.json / release-evidence.md** — written under
  `artifacts/phase1-release/<run_id>/` by every gate run, PASS or FAIL.
  Confirm `git_sha` in the JSON matches the release candidate commit
  exactly, and that neither file contains a database password, bearer
  credential, or signing private key.
- **Central/Node `/readyz` PASS, dual-deployment E2E, offline behavior,
  retry/idempotency, both restore smokes** — all asserted by the same
  release-gate run; see `scripts.evidence["scenarios"]` in
  `release-evidence.json` for per-scenario PASS/FAIL and duration.
- **Deployment templates reviewed** — `deploy/systemd/`, `deploy/caddy/`,
  `deploy/env/` match [deployment-systemd-caddy.md](deployment-systemd-caddy.md)
  and pass `scripts/tests/test_deploy_templates.py`.
- **Operator docs current** — this directory (`docs/operator/`) reflects
  the actual shipped commands/endpoints/templates, not aspirational ones;
  see [troubleshooting.md](troubleshooting.md) for known Phase 1 gaps to
  carry forward (notably: no operator CLI yet for package build/sign or
  Node package import/activation — see [central.md](central.md) and
  [node.md](node.md)).
- **Known Minor backlog recorded** — the three deferred Task 14 Minors
  (FileSyncTransport deterministic output, durable Central
  idempotency-conflict audit trail, claim-path index/schema-version
  hardening) remain recorded in the decision ledger and do not block
  closure unless their severity has materially increased.
