# Testing Strategy and Quality Gates

LinuxLab uses TDD for domain behavior and integration tests for Central/Node boundaries.

## Per-task gate

A task is not complete until:

1. the narrow failing test was observed;
2. the narrow test passes after implementation;
3. related app tests pass;
4. `ruff check .` passes for changed Python code;
5. relevant `manage.py check` commands pass;
6. migrations are created and applied when needed;
7. the Git diff is self-reviewed against the spec and task scope;
8. one task-level commit exists.

## Required test layers

### Unit/domain tests

Use for pure providers, policies, hashing, serialization, and validation rules.

### Django ORM/service tests

Use for version immutability, Attempt/Result/Progress behavior, permissions, publication, and sync inbox/outbox behavior.

### Integration tests

Use for Wagtail page structure, publication dependency resolution, package verification, Node package lifecycle, and API contracts.

### End-to-end Phase 1 tests

Must prove:

```text
Central authoring
→ publish immutable snapshot
→ sign .llpkg
→ Node verify/stage/activate
→ disconnect Internet
→ Node still serves learning content
```

and:

```text
Node local Result
→ Outbox event
→ retry same event
→ Central applies once
→ ACK returned
```

## Security regression tests

At minimum:

- learner question serialization cannot expose answer keys;
- unpublished dependencies cannot enter published packages;
- tampered `.llpkg` is rejected;
- duplicate sync event is idempotent;
- unpublished ActivityVersion cannot receive a learner Attempt.
