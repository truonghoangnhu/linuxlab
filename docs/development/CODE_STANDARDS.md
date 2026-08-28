# LinuxLab Code Standards

## Python

- Python 3.12 for the first implementation baseline.
- Type hints on public service contracts and non-trivial pure functions.
- Keep Django views thin; application rules belong in service/policy/provider modules.
- Avoid generic `utils.py` dumping grounds; name modules by responsibility.
- Prefer explicit domain names over clever abstractions.
- Use `Decimal` for durable scores when persistence requires exact decimal semantics.

## Django module boundaries

Each app owns its models. Cross-module mutation must happen through a documented public service or a domain event.

Bad:

```python
from linuxlab.learning.models import Progress
Progress.objects.filter(...).update(...)
```

from inside an unrelated module.

Preferred:

```python
from linuxlab.learning.services import rebuild_progress
rebuild_progress(...)
```

## Versioned data

- Published definitions are immutable.
- Changes produce a new version.
- Attempt/Result history never points to a mutable "latest" object.
- Cross-instance references use UUID + version.

## Frontend

Phase 1 uses server-rendered Django/Wagtail templates, HTMX where progressive interaction is useful, and Alpine.js only for small local UI state.

Do not introduce a SPA framework in Phase 1.

## Naming

- Models: singular nouns (`ActivityVersion`, `PublicationSnapshot`).
- Services: verbs (`publish_learning_path`, `rebuild_progress`).
- Domain events: dotted past-tense facts (`activity.completed`).
- API paths: versioned `/api/v1/...`.

## Migration discipline

Every model change requires a committed Django migration. Never rewrite an already-applied shared migration to change behavior; create a new migration.
