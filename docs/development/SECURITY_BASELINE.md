# Security Baseline

Phase 1 has no browser shell execution yet, but the architecture must preserve the security boundary required by later phases.

## Secrets

- Never commit `.env` files containing secrets.
- Never commit PostgreSQL passwords.
- Never commit package-signing private keys.
- Node receives trusted public keys only.

## Assessment

- Answer keys remain server-side.
- Learner serialization is an explicit allow-list.
- Never send the full QuestionVersion model as JSON to the browser.

## Packages

- Build only from immutable publication snapshots.
- Include deterministic payload hash.
- Verify hash and Ed25519 signature before inserting imported payload into Node storage.
- Reject malformed or unexpected archive members.

## Identity

- Do not copy Central user password databases to Node.
- Guest progress remains local by default.
- Later offline credentials must be limited in scope and lifetime.

## Future lab execution boundary

When Phase 1.5 begins, arbitrary learner commands must never be executed by the Django web process. Terminal execution belongs behind a dedicated sandbox service/provider with resource limits and isolation.
