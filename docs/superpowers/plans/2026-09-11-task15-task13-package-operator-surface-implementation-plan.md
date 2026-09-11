# Task 15 / Task 13 Corrective A — Package Operator Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a reproducible Phase 1 operator-facing CLI for Central publication/package build and Node verify/import/status/activate/rollback while preserving the existing publication, signing, verification, trust, and installed-package lifecycle semantics.

**Architecture:** Implement two thin Django management-command adapters, `manage_central_package` and `manage_node_package`. Commands expose only existing canonical domain identities, perform sanctioned read-only object resolution where current service signatures require model instances, obtain signing/trust material from fixed external production configuration, and delegate all domain mutation to existing services. Corrective A adds the missing fixed-path Node Ed25519 trust-anchor loader but no trust store, new identity, schema, lifecycle state, transport protocol, or key-management subsystem.

**Tech Stack:** Python 3.12, Django 5.2, Wagtail 7.4, PostgreSQL, cryptography/Ed25519 support already used by LinuxLab, existing LinuxLab publication/package/sync services, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-11-task15-task13-package-operator-surface-design.md`

**Start HEAD for implementation:** `dc717e83da61e9ea28d105c5f76dca14e897d807`

## Real Interface Inventory (verified against the repository, not inferred)

```python
# linuxlab/content/models.py
class LearningPathPage(StableContentPage): ...
# StableContentPage(StableIdentityModel, Page); StableIdentityModel.public_id
# is models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

# linuxlab/publication/models.py
class PublicationSnapshot(models.Model):
    kind = models.CharField(max_length=100)               # str
    source_public_id = models.UUIDField()                 # uuid.UUID
    version = models.PositiveIntegerField(...)             # int, >= 1
    payload = models.JSONField()
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    # unique_together-style constraint: (kind, source_public_id, version)
    # NO public_id field exists on this model.

# linuxlab/publication/snapshots.py
def publish_learning_path(
    learning_path_page, activity_registry, *,
    _on_snapshot_started=None, _on_captured=None,
) -> PublicationSnapshot: ...
# learning_path_page: a LearningPathPage instance (its .pk is trusted, not
# other in-memory fields). Returns the newly inserted PublicationSnapshot.

# linuxlab/publication/package.py
_SUPPORTED_PUBLICATION_KINDS = frozenset({"learning_path"})

class PackageBuildError(Exception): ...
class PackageVerificationError(Exception): ...

@dataclass(frozen=True)
class PackageManifest:
    schema_version: int
    package_uuid: uuid.UUID
    package_type: str
    package_version: int
    created_at: datetime
    payload_sha256: str
    minimum_linuxlab_version: str
    signature_algorithm: str

@dataclass(frozen=True)
class VerifiedPackage:
    manifest: PackageManifest
    package_bytes: bytes
    payload_bytes: bytes

def build_package(snapshot, activity_registry, private_key) -> bytes: ...
# snapshot: a PublicationSnapshot instance (locator/claim, reloaded by pk
# internally). private_key: an Ed25519PrivateKey (cryptography library
# object), never read from settings/DB/file by this function itself.
# package_uuid in the resulting manifest == snapshot.source_public_id;
# package_type == snapshot.kind; package_version == snapshot.version.

def verify_package(package_bytes, activity_registry, trusted_public_key) -> VerifiedPackage: ...
# trusted_public_key: an Ed25519PublicKey (cryptography library object),
# externally supplied by the caller. Pure/offline; never touches the DB.

# linuxlab/publication/signing.py
SIGNATURE_ALGORITHM = "ed25519"
def sign_bytes(message, private_key): ...      # private_key.sign(message)
def verify_signature(message, signature, public_key): ...  # raises
    # cryptography.exceptions.InvalidSignature on failure

# linuxlab/activities/default_registry.py
def build_default_activity_registry() -> ActivityRegistry: ...
# Zero-argument. Explicitly documented: "the one production ActivityRegistry
# builder, used by ordinary runtime/service code ... not a pytest fixture,
# not a release-gate-only construct." No side effects, no DB access itself.

# linuxlab/sync/models.py
STAGED = "staged"; ACTIVE = "active"; ARCHIVED = "archived"

class InstalledPackage(models.Model):
    package_type = models.CharField(max_length=100)        # str
    source_public_id = models.UUIDField()                  # uuid.UUID
    package_version = models.PositiveIntegerField(...)      # int, >= 1
    state = models.CharField(max_length=20, choices=..., default=STAGED)
    previous_package = models.ForeignKey("self", null=True, ...)
    # unique constraints: (package_type, source_public_id, package_version)
    # and one ACTIVE row per (package_type, source_public_id).
    # NO public_id field exists on this model.

# linuxlab/sync/services.py
class PackageImportError(Exception): ...
class PackageIdentityConflict(PackageImportError): ...
class PackageActivationError(Exception): ...
class PackageRollbackError(Exception): ...

def import_package(package_bytes, activity_registry, trusted_public_key) -> InstalledPackage: ...
# Checks settings.LINUXLAB_CAN_IMPORT_PACKAGES itself (fail-closed) BEFORE
# calling verify_package() internally. Returns the newly staged
# InstalledPackage (state="staged"). Never activates.

def activate_package(package) -> InstalledPackage: ...
# package: an InstalledPackage instance already in state "staged" (an
# already-ACTIVE package is an idempotent no-op; any other state raises
# PackageActivationError). Archives the previously active package (if any)
# in the same (package_type, source_public_id) scope, activates `package`,
# and returns the freshly reloaded, now-ACTIVE InstalledPackage.

def rollback_package(package_type, source_public_id) -> InstalledPackage: ...
# Rolls back the current ACTIVE package for (package_type, source_public_id)
# to its recorded previous_package. Raises PackageRollbackError if there is
# no active package, or the active package has no previous_package. Returns
# the reactivated InstalledPackage.

def get_active_package(package_type, source_public_id): ...  # -> InstalledPackage | None

# linuxlab/settings/base.py / central.py / node.py / test.py
LINUXLAB_PROFILE = "central" | "node" | "test"   # NEVER string-compared for authority
LINUXLAB_CAN_AUTHOR = True (central only)
LINUXLAB_CAN_IMPORT_PACKAGES = True (node only)
```

## Global Constraints

- Commands are thin adapters only; no new package/lifecycle business
  semantics.
- No schema migration.
- No `PublicationSnapshot.public_id`. No `InstalledPackage.public_id`. No
  synthetic CLI identity of any kind.
- No local database primary key ever crosses the operator interface.
- No `--publication`, `--installed-package`, `--to-installed-package`,
  `--latest`, `--force`, or any unsafe bypass flag
  (`--unsafe`/`--ignore-state`/`--skip-lock`/`--skip-verification`/
  `--skip-signature`/`--allow-revoked`/`--trust-file`).
- Central publication identity is `(kind, source_public_id, version)`;
  Phase 1 CLI represents it as `--learning-path <UUID> --version <VERSION>`
  with `kind` fixed to `"learning_path"`.
- Installed-artifact identity is
  `(package_type, source_public_id, package_version)`.
- `rollback` calls exactly `rollback_package(package_type, source_public_id)`
  — no explicit target argument.
- `publish` and `build` are separate operations; no implicit chaining.
- `verify` is read-only; `import` always re-verifies independently and never
  trusts a prior `verify` result.
- Node trust comes only from
  `/etc/linuxlab/keys/package-signing-public.key` (fixed, non-configurable
  in production); the file must contain a PEM `SubjectPublicKeyInfo`
  Ed25519 public key.
- No CLI/env/settings redirect for the trust path or the Central signing-key
  path.
- Wrong capability/profile fails before any key loading, package
  verification, or lifecycle mutation.
- A package's own contents can never select or influence which trust key is
  used to authenticate it.
- Release-gate ephemeral signing/trust material (`scripts/release_gate/`) is
  never imported by, or reused as a fallback in, production command code.
- Central's private signing key is never copied to Node and never appears in
  command output, logs, or error messages.
- No key-management command, keyring, multi-key trust store, TOFU, remote
  trust fetch, or Task 14 trust distribution.
- Central `build` output never silently overwrites an existing final file;
  a failed build/export leaves no partial final `.llpkg` and no leaked temp
  file.
- No Central→Node automatic content transport; only an operator-controlled
  file crosses the boundary.
- The CLI-driven E2E acceptance test (Task 7) proves this with two real,
  independently created and migrated ephemeral PostgreSQL databases and
  Central/Node commands each run in their own subprocess under their own
  real settings module — never a single shared test database, never
  `override_settings`/mutable global `settings` standing in for two
  deployments inside one already-initialized Django process.
- `docs/operator/` is not modified in this corrective.
- Deployment templates are not modified (the approved spec does not require
  it).
- Corrective B, Corrective C, and Task 14 are out of scope.

## Planned File Structure

| Action | Path | Responsibility | Task |
|---|---|---|---|
| Modify | `linuxlab/publication/signing.py` | Add `load_package_signing_private_key()` — Central's fixed-path Ed25519 private-key loader | 1 |
| Modify | `linuxlab/publication/tests/test_signing.py` | Tests for the new loader | 1 |
| Create | `linuxlab/sync/trust.py` | Node's fixed-path Ed25519 trust-anchor loader (`load_package_trusted_public_key()`) and its typed configuration exception | 1 |
| Create | `linuxlab/sync/tests/test_trust.py` | Tests for the new Node trust-anchor loader | 1 |
| Create | `linuxlab/publication/management/__init__.py` | Package marker (publication app has no `management/` dir yet) | 2 |
| Create | `linuxlab/publication/management/commands/__init__.py` | Package marker | 2 |
| Create | `linuxlab/publication/management/commands/manage_central_package.py` | `publish` and `build` subcommands | 2, 3 |
| Create | `linuxlab/publication/tests/test_manage_central_package.py` | Command contract + integration tests for `publish` and `build` | 2, 3 |
| Create | `linuxlab/sync/management/commands/manage_node_package.py` | `verify`, `import`, `status`, `activate`, `rollback` subcommands | 4, 5, 6 |
| Create | `linuxlab/sync/tests/test_manage_node_package.py` | Command contract + integration tests for all five Node subcommands | 4, 5, 6 |
| Create | `scripts/tests/_package_operator_subprocess.py` | Test-only subprocess bootstrap: runs one management command (or the authoring fixture) under an explicit profile/database, in its own process | 7 |
| Create | `scripts/tests/test_package_operator_e2e.py` | One real CLI-driven Central→file→Node end-to-end acceptance test, using two independently created/migrated ephemeral PostgreSQL databases | 7 |

No shared "operator framework" module is created: `manage_central_package.py`
and `manage_node_package.py` do not import from each other, and neither
imports a third shared command-utility module, because neither the
capability guard (a two-line `getattr(settings, ..., False)` check, exactly
mirroring `import_package()`'s own existing style) nor the safe-file-output
helper (needed only by `build`) is genuinely shared by both commands.

## Task 1: Production key/trust loaders

**Files:**
- Modify: `linuxlab/publication/signing.py`
- Modify: `linuxlab/publication/tests/test_signing.py`
- Create: `linuxlab/sync/trust.py`
- Create: `linuxlab/sync/tests/test_trust.py`

**Interfaces:**
- Consumes: `cryptography.hazmat.primitives.serialization.load_pem_private_key`,
  `load_pem_public_key`; `cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey`,
  `Ed25519PublicKey`.
- Produces:
  - `linuxlab.publication.signing.load_package_signing_private_key() -> Ed25519PrivateKey`
  - `linuxlab.publication.signing.PackageSigningConfigurationError(Exception)`
  - `linuxlab.sync.trust.load_package_trusted_public_key() -> Ed25519PublicKey`
  - `linuxlab.sync.trust.PackageTrustConfigurationError(Exception)`

### 1a. Node trust-anchor loader (`linuxlab/sync/trust.py`)

- [ ] Write the first failing test in a new file
  `linuxlab/sync/tests/test_trust.py`:

  ```python
  import pytest
  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

  from linuxlab.sync.trust import load_package_trusted_public_key


  def test_valid_ed25519_pem_returns_public_key(tmp_path, monkeypatch):
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

      key_path = tmp_path / "package-signing-public.key"
      public_key = Ed25519PrivateKey.generate().public_key()
      key_path.write_bytes(
          public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
      )
      monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)

      loaded = load_package_trusted_public_key()

      assert isinstance(loaded, Ed25519PublicKey)
  ```

- [ ] Run it and confirm RED for the expected reason:
  `.venv/bin/pytest linuxlab/sync/tests/test_trust.py::test_valid_ed25519_pem_returns_public_key -q`
  — expected failure: `ModuleNotFoundError: No module named 'linuxlab.sync.trust'`
  (the module does not exist yet).

- [ ] Implement the minimal module `linuxlab/sync/trust.py`:

  ```python
  """Node's fixed local Ed25519 trust anchor for verify_package()/
  import_package() (Amendment C, docs/superpowers/specs/
  2026-09-11-task15-task13-package-operator-surface-design.md Section 5a).
  """

  from pathlib import Path

  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
  from cryptography.hazmat.primitives.serialization import load_pem_public_key

  _TRUST_ANCHOR_PATH = Path("/etc/linuxlab/keys/package-signing-public.key")


  class PackageTrustConfigurationError(Exception):
      pass


  def load_package_trusted_public_key():
      try:
          pem_bytes = _TRUST_ANCHOR_PATH.read_bytes()
      except OSError as exc:
          raise PackageTrustConfigurationError(
              f"cannot read Node trust anchor at {_TRUST_ANCHOR_PATH}: {exc.strerror}"
          ) from exc

      if not pem_bytes:
          raise PackageTrustConfigurationError(
              f"Node trust anchor at {_TRUST_ANCHOR_PATH} is empty"
          )

      try:
          public_key = load_pem_public_key(pem_bytes)
      except ValueError as exc:
          raise PackageTrustConfigurationError(
              f"Node trust anchor at {_TRUST_ANCHOR_PATH} is not valid PEM: {exc}"
          ) from exc

      if not isinstance(public_key, Ed25519PublicKey):
          raise PackageTrustConfigurationError(
              f"Node trust anchor at {_TRUST_ANCHOR_PATH} is not an Ed25519 public key "
              f"(got {type(public_key).__name__})"
          )

      return public_key
  ```

- [ ] Run the focused test again and confirm GREEN:
  `.venv/bin/pytest linuxlab/sync/tests/test_trust.py::test_valid_ed25519_pem_returns_public_key -q`

- [ ] Add the remaining negative tests to the same file, one RED→GREEN cycle
  each (each new test starts RED against the module above until the
  corresponding branch is proven to already satisfy it — since the
  implementation above already handles all six failure modes, each test
  should already be GREEN on first run; if any is unexpectedly RED, that is
  a genuine gap to fix before proceeding):

  ```python
  def test_missing_path_raises_configuration_error(tmp_path, monkeypatch):
      monkeypatch.setattr(
          "linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "does-not-exist.key"
      )
      with pytest.raises(PackageTrustConfigurationError):
          load_package_trusted_public_key()


  def test_directory_path_raises_configuration_error(tmp_path, monkeypatch):
      monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path)
      with pytest.raises(PackageTrustConfigurationError):
          load_package_trusted_public_key()


  def test_empty_file_raises_configuration_error(tmp_path, monkeypatch):
      key_path = tmp_path / "empty.key"
      key_path.write_bytes(b"")
      monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
      with pytest.raises(PackageTrustConfigurationError):
          load_package_trusted_public_key()


  def test_malformed_pem_raises_configuration_error(tmp_path, monkeypatch):
      key_path = tmp_path / "malformed.key"
      key_path.write_bytes(b"-----BEGIN PUBLIC KEY-----\nnot valid\n")
      monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
      with pytest.raises(PackageTrustConfigurationError):
          load_package_trusted_public_key()


  def test_rsa_public_key_raises_configuration_error(tmp_path, monkeypatch):
      from cryptography.hazmat.primitives.asymmetric import rsa
      from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

      key_path = tmp_path / "rsa.key"
      rsa_public = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
      key_path.write_bytes(rsa_public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
      monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
      with pytest.raises(PackageTrustConfigurationError):
          load_package_trusted_public_key()


  def test_ecdsa_public_key_raises_configuration_error(tmp_path, monkeypatch):
      from cryptography.hazmat.primitives.asymmetric import ec
      from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

      key_path = tmp_path / "ecdsa.key"
      ec_public = ec.generate_private_key(ec.SECP256R1()).public_key()
      key_path.write_bytes(ec_public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
      monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
      with pytest.raises(PackageTrustConfigurationError):
          load_package_trusted_public_key()


  def test_error_message_never_contains_pem_bytes(tmp_path, monkeypatch):
      key_path = tmp_path / "malformed.key"
      sentinel = b"TEST_TRUST_ANCHOR_SENTINEL_BYTES"
      key_path.write_bytes(sentinel)
      monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", key_path)
      with pytest.raises(PackageTrustConfigurationError) as excinfo:
          load_package_trusted_public_key()
      assert sentinel.decode() not in str(excinfo.value)
  ```

  Import `PackageTrustConfigurationError` at the top of the test file
  alongside `load_package_trusted_public_key`.

- [ ] Run the focused suite: `.venv/bin/pytest linuxlab/sync/tests/test_trust.py -q`
  — expect all tests GREEN.
- [ ] `git diff --check`
- [ ] Inspect the diff for `linuxlab/sync/trust.py` and
  `linuxlab/sync/tests/test_trust.py`.
- [ ] Commit:
  `git add linuxlab/sync/trust.py linuxlab/sync/tests/test_trust.py && git commit -m "feat: add Node package trust-anchor loader"`

**Task 1a STOP conditions:** STOP and report if `load_pem_public_key` cannot
reject a non-Ed25519 key type by `isinstance` check against
`Ed25519PublicKey` (it can — this is a standard `cryptography` library
guarantee), or if any repository convention already defines a
differently-named trust exception that this task should reuse instead of
introducing `PackageTrustConfigurationError`.

### 1b. Central signing-key loader (`linuxlab/publication/signing.py`)

- [ ] Read the current file first (`linuxlab/publication/signing.py`) and
  confirm it still contains only `SIGNATURE_ALGORITHM`, `sign_bytes`, and
  `verify_signature` — no existing loader to avoid duplicating.

- [ ] Write the first failing test, added to
  `linuxlab/publication/tests/test_signing.py`:

  ```python
  def test_load_package_signing_private_key_returns_private_key(tmp_path, monkeypatch):
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      from cryptography.hazmat.primitives.serialization import (
          Encoding, NoEncryption, PrivateFormat,
      )
      from linuxlab.publication.signing import load_package_signing_private_key

      key_path = tmp_path / "package-signing.key"
      private_key = Ed25519PrivateKey.generate()
      key_path.write_bytes(
          private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
      )
      monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)

      loaded = load_package_signing_private_key()

      assert isinstance(loaded, Ed25519PrivateKey)
  ```

- [ ] Run it and confirm RED:
  `.venv/bin/pytest linuxlab/publication/tests/test_signing.py::test_load_package_signing_private_key_returns_private_key -q`
  — expected failure: `ImportError`/`AttributeError` because
  `load_package_signing_private_key` does not exist yet.

- [ ] Implement the minimal addition to `linuxlab/publication/signing.py`
  (append below the existing `verify_signature`):

  ```python
  from pathlib import Path

  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
  from cryptography.hazmat.primitives.serialization import load_pem_private_key

  _SIGNING_KEY_PATH = Path("/etc/linuxlab/keys/package-signing.key")


  class PackageSigningConfigurationError(Exception):
      pass


  def load_package_signing_private_key():
      try:
          pem_bytes = _SIGNING_KEY_PATH.read_bytes()
      except OSError as exc:
          raise PackageSigningConfigurationError(
              f"cannot read Central signing key at {_SIGNING_KEY_PATH}: {exc.strerror}"
          ) from exc

      if not pem_bytes:
          raise PackageSigningConfigurationError(
              f"Central signing key at {_SIGNING_KEY_PATH} is empty"
          )

      try:
          private_key = load_pem_private_key(pem_bytes, password=None)
      except (ValueError, TypeError) as exc:
          raise PackageSigningConfigurationError(
              f"Central signing key at {_SIGNING_KEY_PATH} is not valid PEM: {exc}"
          ) from exc

      if not isinstance(private_key, Ed25519PrivateKey):
          raise PackageSigningConfigurationError(
              f"Central signing key at {_SIGNING_KEY_PATH} is not an Ed25519 "
              f"private key (got {type(private_key).__name__})"
          )

      return private_key
  ```

  Note the import of `Path` at module top-level: place it with the other
  imports at the top of the file rather than duplicating it inline (Ruff's
  import ordering will otherwise flag it).

- [ ] Run the focused test again and confirm GREEN:
  `.venv/bin/pytest linuxlab/publication/tests/test_signing.py::test_load_package_signing_private_key_returns_private_key -q`

- [ ] Add the remaining negative tests to the same file, following the
  identical structure used for the Node loader in 1a (missing path,
  directory path, empty file, malformed PEM, RSA public/private-shaped PEM
  rejected, ECDSA key rejected, error message never contains raw PEM/key
  bytes — substitute `PackageSigningConfigurationError` and
  `linuxlab.publication.signing._SIGNING_KEY_PATH`/
  `load_package_signing_private_key` throughout). Do not skip any of the
  six negative cases; the sentinel-absence test is mandatory for a signing
  *private* key.

- [ ] Run the focused suite: `.venv/bin/pytest linuxlab/publication/tests/test_signing.py -q`
- [ ] `git diff --check`
- [ ] Inspect the diff.
- [ ] Commit:
  `git add linuxlab/publication/signing.py linuxlab/publication/tests/test_signing.py && git commit -m "feat: add Central package signing-key loader"`

**Task 1b STOP conditions:** STOP and report if `load_pem_private_key`
cannot be made to reject a non-Ed25519 private key via `isinstance` (it
can), or if `build_package()`'s actual accepted `private_key` type is
found to differ from a plain `Ed25519PrivateKey` object when re-checked
(re-verify `linuxlab/publication/package.py:381`'s docstring and the call
`sign_bytes(manifest_bytes + payload_bytes, private_key)` at the point of
implementation — `sign_bytes` calls `private_key.sign(message)`, which is
exactly the `Ed25519PrivateKey.sign` interface).

## Task 2: Central `publish` command

**Files:**
- Create: `linuxlab/publication/management/__init__.py` (empty)
- Create: `linuxlab/publication/management/commands/__init__.py` (empty)
- Create: `linuxlab/publication/management/commands/manage_central_package.py`
- Create: `linuxlab/publication/tests/test_manage_central_package.py`

**Interfaces:**
- Consumes: `linuxlab.content.models.LearningPathPage`,
  `linuxlab.publication.snapshots.publish_learning_path(learning_path_page, activity_registry)`,
  `linuxlab.activities.default_registry.build_default_activity_registry()`,
  `settings.LINUXLAB_CAN_AUTHOR`.
- Produces: `manage.py manage_central_package publish --learning-path <UUID>`,
  exit 0 on success with stdout lines `Operation:`, `Kind:`, `LearningPath:`,
  `Version:`; non-zero `CommandError` on failure.

- [ ] Create the two empty `__init__.py` files first:
  `linuxlab/publication/management/__init__.py` and
  `linuxlab/publication/management/commands/__init__.py`.

- [ ] Write the first failing test in a new file
  `linuxlab/publication/tests/test_manage_central_package.py`:

  ```python
  import uuid

  import pytest
  from django.core.management import call_command
  from django.core.management.base import CommandError

  pytestmark = pytest.mark.django_db


  def _make_learning_path(title="Intro to Bash"):
      from linuxlab.content.models import LearningPathPage
      from wagtail.models import Page

      root = Page.objects.get(depth=1)
      path = LearningPathPage(title=title, slug=title.lower().replace(" ", "-"))
      root.add_child(instance=path)
      return path


  def test_publish_reports_kind_learning_path_and_version(settings, capsys):
      settings.LINUXLAB_CAN_AUTHOR = True
      path = _make_learning_path()

      call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

      out = capsys.readouterr().out
      assert "Operation: publish" in out
      assert "Kind: learning_path" in out
      assert f"LearningPath: {path.public_id}" in out
      assert "Version: 1" in out
      assert "Publication:" not in out
  ```

- [ ] Run it and confirm RED:
  `.venv/bin/pytest linuxlab/publication/tests/test_manage_central_package.py::test_publish_reports_kind_learning_path_and_version -q`
  — expected failure: `django.core.management.base.CommandError: Unknown
  command: 'manage_central_package'` (the command does not exist yet).

- [ ] Implement the minimal command in
  `linuxlab/publication/management/commands/manage_central_package.py`:

  ```python
  import uuid

  from django.conf import settings
  from django.core.management.base import BaseCommand, CommandError

  from linuxlab.activities.default_registry import build_default_activity_registry
  from linuxlab.content.models import LearningPathPage
  from linuxlab.publication.snapshots import publish_learning_path


  def _require_central_capability():
      if not getattr(settings, "LINUXLAB_CAN_AUTHOR", False):
          raise CommandError(
              "package publication/build is not permitted on this deployment profile"
          )


  def _parse_uuid(raw, *, what):
      try:
          return uuid.UUID(raw)
      except (ValueError, AttributeError, TypeError) as exc:
          raise CommandError(f"{what} is not a valid UUID: {raw!r}") from exc


  class Command(BaseCommand):
      help = "Central package operator surface: publish and build."

      def add_arguments(self, parser):
          subparsers = parser.add_subparsers(dest="action", required=True)

          publish_parser = subparsers.add_parser("publish")
          publish_parser.add_argument("--learning-path", required=True)

      def handle(self, *args, **options):
          _require_central_capability()

          if options["action"] == "publish":
              self._handle_publish(options)
              return

      def _handle_publish(self, options):
          learning_path_id = _parse_uuid(options["learning_path"], what="--learning-path")

          try:
              page = LearningPathPage.objects.get(public_id=learning_path_id)
          except LearningPathPage.DoesNotExist as exc:
              raise CommandError(f"LearningPath not found: {learning_path_id}") from exc

          registry = build_default_activity_registry()
          snapshot = publish_learning_path(page, registry)

          self.stdout.write("Operation: publish")
          self.stdout.write(f"Kind: {snapshot.kind}")
          self.stdout.write(f"LearningPath: {snapshot.source_public_id}")
          self.stdout.write(f"Version: {snapshot.version}")
  ```

- [ ] Run the focused test again and confirm GREEN:
  `.venv/bin/pytest linuxlab/publication/tests/test_manage_central_package.py::test_publish_reports_kind_learning_path_and_version -q`

- [ ] Add the remaining `publish` tests to the same file, one RED→GREEN
  cycle each:

  ```python
  def test_publish_rejects_malformed_uuid(settings):
      settings.LINUXLAB_CAN_AUTHOR = True
      with pytest.raises(CommandError):
          call_command("manage_central_package", "publish", "--learning-path", "not-a-uuid")


  def test_publish_rejects_unknown_learning_path(settings):
      settings.LINUXLAB_CAN_AUTHOR = True
      with pytest.raises(CommandError):
          call_command(
              "manage_central_package", "publish", "--learning-path", str(uuid.uuid4())
          )


  def test_publish_rejects_wrong_profile_before_mutation(settings):
      from linuxlab.publication.models import PublicationSnapshot

      settings.LINUXLAB_CAN_AUTHOR = False
      path = _make_learning_path()

      with pytest.raises(CommandError):
          call_command(
              "manage_central_package", "publish", "--learning-path", str(path.public_id)
          )

      assert not PublicationSnapshot.objects.filter(source_public_id=path.public_id).exists()


  def test_publish_second_call_allocates_next_version(settings, capsys):
      settings.LINUXLAB_CAN_AUTHOR = True
      path = _make_learning_path()

      call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))
      capsys.readouterr()
      call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

      out = capsys.readouterr().out
      assert "Version: 2" in out
  ```

  This last test documents (rather than invents) `publish_learning_path`'s
  own existing idempotency-free version-allocation behavior; it is not new
  CLI-level logic.

- [ ] Run the focused suite: `.venv/bin/pytest linuxlab/publication/tests/test_manage_central_package.py -q`
- [ ] `git diff --check`
- [ ] Inspect the diff.
- [ ] Commit:
  `git add linuxlab/publication/management linuxlab/publication/tests/test_manage_central_package.py && git commit -m "feat: add Central package operator publish command"`

**Task 2 STOP conditions:** STOP and report if `LearningPathPage.objects.get(public_id=...)`
does not exist as a queryable manager path (it does — confirmed via
`linuxlab/publication/tests/test_package.py:421`'s existing use of the
identical pattern), or if `publish_learning_path` requires an argument
beyond `(learning_path_page, activity_registry)` for ordinary production
use (the optional `_on_snapshot_started`/`_on_captured` keyword-only
parameters are test-only synchronization seams, always `None` in
production, and must not be passed by the command).

## Task 3: Central `build` command (safe atomic export)

**Files:**
- Modify: `linuxlab/publication/management/commands/manage_central_package.py`
- Modify: `linuxlab/publication/tests/test_manage_central_package.py`

**Interfaces:**
- Consumes: `linuxlab.publication.models.PublicationSnapshot`,
  `linuxlab.publication.package.build_package(snapshot, activity_registry, private_key)`,
  `linuxlab.publication.signing.load_package_signing_private_key()` (Task 1b).
- Produces: `manage.py manage_central_package build --learning-path <UUID>
  --version <VERSION> --output <FILE.llpkg>`; the new private helper
  `_write_new_file_atomically(destination, data)` inside the command module.

### 3a. Safe non-overwriting file publication helper

- [ ] Write the first failing test, appended to
  `linuxlab/publication/tests/test_manage_central_package.py`:

  ```python
  def test_write_new_file_atomically_creates_file_with_exact_bytes(tmp_path):
      from linuxlab.publication.management.commands.manage_central_package import (
          _write_new_file_atomically,
      )

      destination = tmp_path / "out.llpkg"
      _write_new_file_atomically(destination, b"package-bytes")

      assert destination.read_bytes() == b"package-bytes"
  ```

- [ ] Run it and confirm RED:
  `.venv/bin/pytest linuxlab/publication/tests/test_manage_central_package.py::test_write_new_file_atomically_creates_file_with_exact_bytes -q`
  — expected failure: `ImportError` (the helper does not exist yet).

- [ ] Implement the minimal helper in
  `linuxlab/publication/management/commands/manage_central_package.py`
  (add near the top of the module, after the imports):

  ```python
  import os


  def _write_new_file_atomically(destination, data: bytes) -> None:
      """Publish `data` at `destination` without ever overwriting an
      existing file there, and without ever leaving a partial final file
      or a leaked temp file on any failure path.

      Strategy: write the full bytes to a temp file in the SAME directory
      as `destination` (so the later link is a same-filesystem, atomic
      operation), fsync+close it, then `os.link()` the temp file onto the
      final path. `os.link()` raises FileExistsError atomically if the
      destination already exists (unlike `os.replace()`, which silently
      overwrites) -- this closes the TOCTOU race an `if not path.exists()`
      check followed by a separate write/rename would leave open. The temp
      file is always removed in `finally`, whether the link succeeded or
      not, so no leaked temp artifact remains on any exit path.
      """
      parent = destination.parent
      temp_path = parent / f".{destination.name}.tmp-{os.getpid()}-{id(data)}"

      fd = os.open(str(temp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
      try:
          with os.fdopen(fd, "wb") as handle:
              handle.write(data)
              handle.flush()
              os.fsync(handle.fileno())

          try:
              os.link(str(temp_path), str(destination))
          except FileExistsError as exc:
              raise CommandError(f"output already exists: {destination}") from exc
      finally:
          temp_path.unlink(missing_ok=True)
  ```

  Note: `os.fdopen(fd, "wb")` takes ownership of `fd`; the `with` block's
  own close is the only close, so there is no double-close. If
  `os.open(..., O_EXCL, ...)` itself raises `FileExistsError` (a second
  concurrent invocation racing on the same temp name — practically
  impossible given the pid+id-based name, but defensively correct), that
  exception propagates unchanged; it is not the "output already exists"
  case (that check is only meaningful for `destination`, not `temp_path`).

- [ ] Run the focused test again and confirm GREEN:
  `.venv/bin/pytest linuxlab/publication/tests/test_manage_central_package.py::test_write_new_file_atomically_creates_file_with_exact_bytes -q`

- [ ] Add the remaining helper tests:

  ```python
  def test_write_new_file_atomically_refuses_existing_destination(tmp_path):
      from linuxlab.publication.management.commands.manage_central_package import (
          _write_new_file_atomically,
      )

      destination = tmp_path / "out.llpkg"
      destination.write_bytes(b"original")

      with pytest.raises(CommandError):
          _write_new_file_atomically(destination, b"new-bytes")

      assert destination.read_bytes() == b"original"


  def test_write_new_file_atomically_leaves_no_temp_file_on_existing_destination(tmp_path):
      from linuxlab.publication.management.commands.manage_central_package import (
          _write_new_file_atomically,
      )

      destination = tmp_path / "out.llpkg"
      destination.write_bytes(b"original")

      with pytest.raises(CommandError):
          _write_new_file_atomically(destination, b"new-bytes")

      remaining = list(tmp_path.iterdir())
      assert remaining == [destination]
  ```

- [ ] Run: `.venv/bin/pytest linuxlab/publication/tests/test_manage_central_package.py -k write_new_file_atomically -q`
- [ ] `git diff --check`; inspect the diff.
- [ ] Commit:
  `git add linuxlab/publication/management/commands/manage_central_package.py linuxlab/publication/tests/test_manage_central_package.py && git commit -m "feat: add fail-closed atomic package export helper"`

### 3b. `build` command

- [ ] Write the first failing integration test:

  ```python
  def _publish_and_capture(settings, path):
      settings.LINUXLAB_CAN_AUTHOR = True
      call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))


  def test_build_produces_verifiable_package(settings, tmp_path, monkeypatch):
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      from cryptography.hazmat.primitives.serialization import (
          Encoding, NoEncryption, PrivateFormat,
      )
      from linuxlab.activities.default_registry import build_default_activity_registry
      from linuxlab.publication.package import verify_package

      settings.LINUXLAB_CAN_AUTHOR = True
      path = _make_learning_path()
      call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

      private_key = Ed25519PrivateKey.generate()
      key_path = tmp_path / "package-signing.key"
      key_path.write_bytes(
          private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
      )
      monkeypatch.setattr(
          "linuxlab.publication.signing._SIGNING_KEY_PATH", key_path
      )

      output_path = tmp_path / "out.llpkg"
      call_command(
          "manage_central_package", "build",
          "--learning-path", str(path.public_id),
          "--version", "1",
          "--output", str(output_path),
      )

      verified = verify_package(
          output_path.read_bytes(), build_default_activity_registry(), private_key.public_key()
      )
      assert str(verified.manifest.package_uuid) == str(path.public_id)
      assert verified.manifest.package_type == "learning_path"
      assert verified.manifest.package_version == 1
  ```

- [ ] Run it and confirm RED:
  `.venv/bin/pytest linuxlab/publication/tests/test_manage_central_package.py::test_build_produces_verifiable_package -q`
  — expected failure: `CommandError: unrecognized arguments` /
  `argparse` error, since the `build` subparser does not exist yet.

- [ ] Implement `build` in
  `linuxlab/publication/management/commands/manage_central_package.py`.
  Extend `add_arguments` and `handle`:

  ```python
  from linuxlab.publication.models import PublicationSnapshot
  from linuxlab.publication.package import build_package
  from linuxlab.publication.signing import load_package_signing_private_key
  ```

  ```python
      def add_arguments(self, parser):
          subparsers = parser.add_subparsers(dest="action", required=True)

          publish_parser = subparsers.add_parser("publish")
          publish_parser.add_argument("--learning-path", required=True)

          build_parser = subparsers.add_parser("build")
          build_parser.add_argument("--learning-path", required=True)
          build_parser.add_argument("--version", required=True, type=int)
          build_parser.add_argument("--output", required=True)

      def handle(self, *args, **options):
          _require_central_capability()

          if options["action"] == "publish":
              self._handle_publish(options)
              return

          if options["action"] == "build":
              self._handle_build(options)
              return
  ```

  ```python
      def _handle_build(self, options):
          from pathlib import Path

          source_public_id = _parse_uuid(options["learning_path"], what="--learning-path")
          version = options["version"]

          try:
              snapshot = PublicationSnapshot.objects.get(
                  kind="learning_path", source_public_id=source_public_id, version=version
              )
          except PublicationSnapshot.DoesNotExist as exc:
              raise CommandError(
                  f"no publication for LearningPath {source_public_id} at version {version}"
              ) from exc

          output_path = Path(options["output"])
          if not output_path.parent.is_dir():
              raise CommandError(f"output directory does not exist: {output_path.parent}")
          if output_path.exists():
              raise CommandError(f"output already exists: {output_path}")

          try:
              private_key = load_package_signing_private_key()
          except PackageSigningConfigurationError as exc:
              raise CommandError(f"signing configuration unavailable: {exc}") from exc

          registry = build_default_activity_registry()
          package_bytes = build_package(snapshot, registry, private_key)

          _write_new_file_atomically(output_path, package_bytes)

          from linuxlab.publication.package import _parse_manifest  # see note below

          self.stdout.write("Operation: build")
          self.stdout.write(f"Kind: {snapshot.kind}")
          self.stdout.write(f"LearningPath: {snapshot.source_public_id}")
          self.stdout.write(f"Version: {snapshot.version}")
          self.stdout.write(f"PackageType: {snapshot.kind}")
          self.stdout.write(f"PackageVersion: {snapshot.version}")
          self.stdout.write(f"PackageUUID: {snapshot.source_public_id}")
          self.stdout.write(f"Output: {output_path}")
  ```

  Also add the import at the top of the file:
  `from linuxlab.publication.signing import PackageSigningConfigurationError`

  Do not actually import or call the private `_parse_manifest` helper shown
  above as a stray line — remove it. The manifest fields reported
  (`PackageUUID`/`PackageType`/`PackageVersion`) are already fully known
  from `snapshot` itself per `build_package`'s own documented mapping
  (`package_uuid == source_public_id`, `package_type == kind`,
  `package_version == version` — Section 2a, layer C of the spec), so no
  additional manifest parsing is required; delete that stray line before
  running the test.

- [ ] Run the focused test again and confirm GREEN:
  `.venv/bin/pytest linuxlab/publication/tests/test_manage_central_package.py::test_build_produces_verifiable_package -q`

- [ ] Add the remaining `build` tests, one RED→GREEN cycle each (reusing the
  `monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", ...)`
  pattern from the happy-path test above):

  ```python
  def test_build_rejects_malformed_learning_path_uuid(settings):
      settings.LINUXLAB_CAN_AUTHOR = True
      with pytest.raises(CommandError):
          call_command(
              "manage_central_package", "build",
              "--learning-path", "not-a-uuid", "--version", "1", "--output", "/tmp/x.llpkg",
          )


  def test_build_rejects_unknown_learning_path(settings, tmp_path):
      settings.LINUXLAB_CAN_AUTHOR = True
      with pytest.raises(CommandError):
          call_command(
              "manage_central_package", "build",
              "--learning-path", str(uuid.uuid4()), "--version", "1",
              "--output", str(tmp_path / "out.llpkg"),
          )


  def test_build_rejects_nonexistent_version(settings, tmp_path):
      settings.LINUXLAB_CAN_AUTHOR = True
      path = _make_learning_path()
      call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

      with pytest.raises(CommandError):
          call_command(
              "manage_central_package", "build",
              "--learning-path", str(path.public_id), "--version", "2",
              "--output", str(tmp_path / "out.llpkg"),
          )


  def test_build_does_not_select_a_different_version(settings, tmp_path, monkeypatch):
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      from cryptography.hazmat.primitives.serialization import (
          Encoding, NoEncryption, PrivateFormat,
      )

      settings.LINUXLAB_CAN_AUTHOR = True
      path = _make_learning_path()
      call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))
      call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

      private_key = Ed25519PrivateKey.generate()
      key_path = tmp_path / "package-signing.key"
      key_path.write_bytes(
          private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
      )
      monkeypatch.setattr("linuxlab.publication.signing._SIGNING_KEY_PATH", key_path)

      output_path = tmp_path / "out.llpkg"
      call_command(
          "manage_central_package", "build",
          "--learning-path", str(path.public_id), "--version", "1", "--output", str(output_path),
      )

      from linuxlab.activities.default_registry import build_default_activity_registry
      from linuxlab.publication.package import verify_package

      verified = verify_package(
          output_path.read_bytes(), build_default_activity_registry(), private_key.public_key()
      )
      assert verified.manifest.package_version == 1


  def test_build_fails_before_file_work_on_wrong_profile(settings, tmp_path):
      settings.LINUXLAB_CAN_AUTHOR = False
      output_path = tmp_path / "out.llpkg"

      with pytest.raises(CommandError):
          call_command(
              "manage_central_package", "build",
              "--learning-path", str(uuid.uuid4()), "--version", "1",
              "--output", str(output_path),
          )

      assert not output_path.exists()


  def test_build_fails_closed_on_missing_signing_configuration(settings, tmp_path, monkeypatch):
      settings.LINUXLAB_CAN_AUTHOR = True
      path = _make_learning_path()
      call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

      monkeypatch.setattr(
          "linuxlab.publication.signing._SIGNING_KEY_PATH", tmp_path / "missing.key"
      )
      output_path = tmp_path / "out.llpkg"

      with pytest.raises(CommandError):
          call_command(
              "manage_central_package", "build",
              "--learning-path", str(path.public_id), "--version", "1",
              "--output", str(output_path),
          )

      assert not output_path.exists()


  def test_build_rejects_invalid_output_parent(settings, tmp_path):
      settings.LINUXLAB_CAN_AUTHOR = True
      path = _make_learning_path()
      call_command("manage_central_package", "publish", "--learning-path", str(path.public_id))

      with pytest.raises(CommandError):
          call_command(
              "manage_central_package", "build",
              "--learning-path", str(path.public_id), "--version", "1",
              "--output", str(tmp_path / "does-not-exist" / "out.llpkg"),
          )
  ```

- [ ] Run the full command test suite:
  `.venv/bin/pytest linuxlab/publication/tests/test_manage_central_package.py -q`
- [ ] Run the focused publication regression suite:
  `.venv/bin/pytest linuxlab/publication/tests/ -q`
- [ ] `git diff --check`; inspect the diff.
- [ ] Commit:
  `git add linuxlab/publication/management/commands/manage_central_package.py linuxlab/publication/tests/test_manage_central_package.py && git commit -m "feat: add Central package build/sign/export command"`

**Task 3 STOP conditions:** STOP and report if `os.link()` is unavailable
on the target filesystem (e.g. an actual cross-filesystem `--output`
destination in a real deployment — this plan's helper assumes `--output`'s
parent directory and a same-directory temp file share one filesystem,
which is the normal case for a local operator-selected path; if a future
real usage needs a genuinely different filesystem, that is an operational
concern for Corrective C's documentation, not a Corrective A code change),
or if `PublicationSnapshot.objects.get(kind=..., source_public_id=...,
version=...)` is found not to be a valid read-only query against the
guarded manager (it is — `PublicationSnapshotQuerySet` only overrides
`bulk_create`/`update`/`bulk_update`, never `get`/`filter`).

## Task 4: Node `verify` and `import` commands

**Files:**
- Create: `linuxlab/sync/management/commands/manage_node_package.py`
- Create: `linuxlab/sync/tests/test_manage_node_package.py`

**Interfaces:**
- Consumes: `linuxlab.sync.trust.load_package_trusted_public_key()` (Task 1a),
  `linuxlab.publication.package.verify_package(package_bytes, activity_registry, trusted_public_key)`,
  `linuxlab.sync.services.import_package(package_bytes, activity_registry, trusted_public_key)`,
  `linuxlab.activities.default_registry.build_default_activity_registry()`,
  `settings.LINUXLAB_CAN_IMPORT_PACKAGES`.
- Produces: `manage.py manage_node_package verify --file <FILE.llpkg>` and
  `manage.py manage_node_package import --file <FILE.llpkg>`.

- [ ] Establish a shared test fixture at the top of the new file
  `linuxlab/sync/tests/test_manage_node_package.py` — a real signed
  `.llpkg` built the same way Task 3's tests do, so `verify`/`import` tests
  exercise a genuine artifact rather than a hand-built one:

  ```python
  import uuid

  import pytest
  from django.core.management import call_command
  from django.core.management.base import CommandError

  pytestmark = pytest.mark.django_db


  @pytest.fixture
  def signed_package(tmp_path, monkeypatch):
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      from cryptography.hazmat.primitives.serialization import (
          Encoding, NoEncryption, PrivateFormat, PublicFormat,
      )
      from linuxlab.activities.default_registry import build_default_activity_registry
      from linuxlab.content.models import LearningPathPage
      from linuxlab.publication.package import build_package
      from linuxlab.publication.snapshots import publish_learning_path
      from wagtail.models import Page

      root = Page.objects.get(depth=1)
      path = LearningPathPage(title="Intro", slug=f"intro-{uuid.uuid4().hex[:8]}")
      root.add_child(instance=path)

      registry = build_default_activity_registry()
      snapshot = publish_learning_path(path, registry)

      private_key = Ed25519PrivateKey.generate()
      package_bytes = build_package(snapshot, registry, private_key)

      trust_path = tmp_path / "package-signing-public.key"
      trust_path.write_bytes(
          private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
      )
      monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", trust_path)

      package_path = tmp_path / "package.llpkg"
      package_path.write_bytes(package_bytes)

      return {
          "path": package_path,
          "bytes": package_bytes,
          "source_public_id": snapshot.source_public_id,
          "version": snapshot.version,
          "private_key": private_key,
          "trust_path": trust_path,
      }
  ```

- [ ] Write the first failing test:

  ```python
  def test_verify_succeeds_on_valid_package(settings, signed_package, capsys):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

      call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

      out = capsys.readouterr().out
      assert "Operation: verify" in out
      assert f"Source: {signed_package['source_public_id']}" in out
  ```

- [ ] Run it and confirm RED:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py::test_verify_succeeds_on_valid_package -q`
  — expected failure: `Unknown command: 'manage_node_package'`.

- [ ] Implement the minimal command in
  `linuxlab/sync/management/commands/manage_node_package.py`:

  ```python
  import uuid
  from pathlib import Path

  from django.conf import settings
  from django.core.management.base import BaseCommand, CommandError

  from linuxlab.activities.default_registry import build_default_activity_registry
  from linuxlab.publication.package import PackageVerificationError, verify_package
  from linuxlab.sync.services import PackageImportError, import_package
  from linuxlab.sync.trust import PackageTrustConfigurationError, load_package_trusted_public_key


  def _require_node_capability():
      if not getattr(settings, "LINUXLAB_CAN_IMPORT_PACKAGES", False):
          raise CommandError(
              "package verify/import is not permitted on this deployment profile"
          )


  def _load_trust_anchor():
      try:
          return load_package_trusted_public_key()
      except PackageTrustConfigurationError as exc:
          raise CommandError(f"trust anchor unavailable: {exc}") from exc


  class Command(BaseCommand):
      help = "Node package operator surface: verify, import, status, activate, rollback."

      def add_arguments(self, parser):
          subparsers = parser.add_subparsers(dest="action", required=True)

          verify_parser = subparsers.add_parser("verify")
          verify_parser.add_argument("--file", required=True)

          import_parser = subparsers.add_parser("import")
          import_parser.add_argument("--file", required=True)

      def handle(self, *args, **options):
          _require_node_capability()

          if options["action"] == "verify":
              self._handle_verify(options)
              return

          if options["action"] == "import":
              self._handle_import(options)
              return

      def _handle_verify(self, options):
          trusted_public_key = _load_trust_anchor()
          package_bytes = Path(options["file"]).read_bytes()
          registry = build_default_activity_registry()

          try:
              verified = verify_package(package_bytes, registry, trusted_public_key)
          except PackageVerificationError as exc:
              raise CommandError(f"package verification failed: {exc}") from exc

          self.stdout.write("Operation: verify")
          self.stdout.write(f"PackageType: {verified.manifest.package_type}")
          self.stdout.write(f"Source: {verified.manifest.package_uuid}")
          self.stdout.write(f"Version: {verified.manifest.package_version}")
  ```

- [ ] Run the focused test again and confirm GREEN:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py::test_verify_succeeds_on_valid_package -q`

- [ ] Add the remaining `verify` tests, one RED→GREEN cycle each:

  ```python
  def test_verify_causes_zero_installed_package_mutation(settings, signed_package):
      from linuxlab.sync.models import InstalledPackage

      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))
      assert InstalledPackage.objects.count() == 0


  def test_verify_rejects_wrong_key(settings, signed_package, tmp_path, monkeypatch):
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      wrong_public = Ed25519PrivateKey.generate().public_key()
      wrong_trust_path = tmp_path / "wrong-public.key"
      wrong_trust_path.write_bytes(
          wrong_public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
      )
      monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", wrong_trust_path)

      with pytest.raises(CommandError):
          call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))


  def test_verify_rejects_tampered_package(settings, signed_package):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      tampered_path = signed_package["path"].with_name("tampered.llpkg")
      tampered_path.write_bytes(signed_package["bytes"][:-1] + b"\x00")

      with pytest.raises(CommandError):
          call_command("manage_node_package", "verify", "--file", str(tampered_path))


  def test_verify_rejects_malformed_archive(settings, tmp_path, signed_package):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      not_a_package = tmp_path / "not-a-package.llpkg"
      not_a_package.write_bytes(b"not a zip archive")

      with pytest.raises(CommandError):
          call_command("manage_node_package", "verify", "--file", str(not_a_package))


  def test_verify_rejects_missing_trust_anchor(settings, signed_package, tmp_path, monkeypatch):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      monkeypatch.setattr(
          "linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "does-not-exist.key"
      )
      with pytest.raises(CommandError):
          call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))


  def test_verify_rejects_wrong_profile_before_trust_loading(
      settings, signed_package, monkeypatch
  ):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
      called = []
      monkeypatch.setattr(
          "linuxlab.sync.management.commands.manage_node_package.load_package_trusted_public_key",
          lambda: called.append(True),
      )

      with pytest.raises(CommandError):
          call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

      assert called == []
  ```

- [ ] Run: `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py -k verify -q`
- [ ] `git diff --check`; inspect the diff.
- [ ] Commit:
  `git add linuxlab/sync/management/commands/manage_node_package.py linuxlab/sync/tests/test_manage_node_package.py && git commit -m "feat: add Node package verify command"`

Now `import`:

- [ ] Write the first failing test:

  ```python
  def test_import_stages_package_and_reports_canonical_identity(settings, signed_package, capsys):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

      call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

      out = capsys.readouterr().out
      assert "Operation: import" in out
      assert f"Source: {signed_package['source_public_id']}" in out
      assert f"Version: {signed_package['version']}" in out
      assert "State: staged" in out
  ```

- [ ] Run it and confirm RED:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py::test_import_stages_package_and_reports_canonical_identity -q`
  — expected failure: `CommandError: unrecognized arguments` (the `import`
  subparser does not exist yet).

- [ ] Implement `import`. Extend `add_arguments` and `handle`:

  ```python
          import_parser = subparsers.add_parser("import")
          import_parser.add_argument("--file", required=True)
  ```

  (already added above in Task 4's initial skeleton for `verify` — confirm
  it is present, then add the dispatch branch in `handle` if not already
  there, and the handler method):

  ```python
      def _handle_import(self, options):
          trusted_public_key = _load_trust_anchor()
          package_bytes = Path(options["file"]).read_bytes()
          registry = build_default_activity_registry()

          try:
              installed = import_package(package_bytes, registry, trusted_public_key)
          except PackageImportError as exc:
              raise CommandError(f"package import failed: {exc}") from exc

          self.stdout.write("Operation: import")
          self.stdout.write(f"PackageType: {installed.package_type}")
          self.stdout.write(f"Source: {installed.source_public_id}")
          self.stdout.write(f"Version: {installed.package_version}")
          self.stdout.write(f"State: {installed.state}")
  ```

- [ ] Run the focused test again and confirm GREEN:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py::test_import_stages_package_and_reports_canonical_identity -q`

- [ ] Add the remaining `import` tests, including the mandatory adversarial
  test proving `import` never trusts a prior `verify`:

  ```python
  def test_import_rejects_wrong_key_with_zero_mutation(
      settings, signed_package, tmp_path, monkeypatch
  ):
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
      from linuxlab.sync.models import InstalledPackage

      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      wrong_public = Ed25519PrivateKey.generate().public_key()
      wrong_trust_path = tmp_path / "wrong-public.key"
      wrong_trust_path.write_bytes(
          wrong_public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
      )
      monkeypatch.setattr("linuxlab.sync.trust._TRUST_ANCHOR_PATH", wrong_trust_path)

      with pytest.raises(CommandError):
          call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

      assert InstalledPackage.objects.count() == 0


  def test_import_rejects_missing_trust_configuration_with_zero_mutation(
      settings, signed_package, tmp_path, monkeypatch
  ):
      from linuxlab.sync.models import InstalledPackage

      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      monkeypatch.setattr(
          "linuxlab.sync.trust._TRUST_ANCHOR_PATH", tmp_path / "does-not-exist.key"
      )

      with pytest.raises(CommandError):
          call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

      assert InstalledPackage.objects.count() == 0


  def test_verify_then_tamper_then_import_fails(settings, signed_package):
      """Adversarial test (spec Section 16): a prior `verify` result is
      never a trusted cache. `import` always re-verifies the exact bytes
      on disk at the moment it runs."""
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True

      call_command("manage_node_package", "verify", "--file", str(signed_package["path"]))

      signed_package["path"].write_bytes(signed_package["bytes"][:-1] + b"\x00")

      with pytest.raises(CommandError):
          call_command("manage_node_package", "import", "--file", str(signed_package["path"]))


  def test_import_fails_before_trust_loading_on_wrong_profile(
      settings, signed_package, monkeypatch
  ):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
      called = []
      monkeypatch.setattr(
          "linuxlab.sync.management.commands.manage_node_package.load_package_trusted_public_key",
          lambda: called.append(True),
      )

      with pytest.raises(CommandError):
          call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

      assert called == []
  ```

- [ ] Run the focused command suite:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py -q`
- [ ] Run the sync app regression suite:
  `.venv/bin/pytest linuxlab/sync/tests/ -q`
- [ ] `git diff --check`; inspect the diff.
- [ ] Commit:
  `git add linuxlab/sync/management/commands/manage_node_package.py linuxlab/sync/tests/test_manage_node_package.py && git commit -m "feat: add Node package import command"`

**Task 4 STOP conditions:** STOP and report if `import_package`'s own
internal `settings.LINUXLAB_CAN_IMPORT_PACKAGES` check (inside
`linuxlab/sync/services.py`) and the command's own capability guard are
found to disagree in any way that would let the command proceed past its
own guard while the service still silently no-ops rather than raising (it
does not — both read the identical setting, and the service raises loudly
via `PackageImportError`), or if `verify_package`/`import_package` are
found to require any additional production-unavailable input beyond
`(package_bytes, activity_registry, trusted_public_key)`.

## Task 5: Node `status` command

**Files:**
- Modify: `linuxlab/sync/management/commands/manage_node_package.py`
- Modify: `linuxlab/sync/tests/test_manage_node_package.py`

**Interfaces:**
- Consumes: `linuxlab.sync.models.InstalledPackage.objects` (read-only
  `filter()`/`order_by()`, never `.update()`/`.create()`).
- Produces: `manage.py manage_node_package status [--package-type <TYPE>]
  [--source-public-id <UUID>] [--package-version <VERSION>]`.

Filter-combination rule (decided per spec Section 8 — "no ambiguous
matching"): no arguments lists everything; `--source-public-id` alone or
together with `--package-type` narrows to that lineage; the full three-field
tuple narrows to exactly one row. `--package-version` is accepted only
together with `--source-public-id` (a version number alone, or a
type+version pair without a source, is not a meaningful filter and is
rejected with a `CommandError` before querying).

- [ ] Write the first failing test:

  ```python
  def test_status_with_no_args_lists_installed_packages_deterministically(
      settings, signed_package, capsys
  ):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
      capsys.readouterr()

      call_command("manage_node_package", "status")

      out = capsys.readouterr().out
      assert f"Source: {signed_package['source_public_id']}" in out
      assert f"Version: {signed_package['version']}" in out
      assert "State: staged" in out
  ```

- [ ] Run it and confirm RED:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py::test_status_with_no_args_lists_installed_packages_deterministically -q`
  — expected failure: `CommandError: unrecognized arguments` (the `status`
  subparser does not exist yet).

- [ ] Implement `status`. Add to `add_arguments`:

  ```python
          status_parser = subparsers.add_parser("status")
          status_parser.add_argument("--package-type")
          status_parser.add_argument("--source-public-id")
          status_parser.add_argument("--package-version", type=int)
  ```

  Add the dispatch branch in `handle` and the handler:

  ```python
      def _handle_status(self, options):
          from linuxlab.sync.models import InstalledPackage

          package_type = options["package_type"]
          source_public_id_raw = options["source_public_id"]
          package_version = options["package_version"]

          if package_version is not None and source_public_id_raw is None:
              raise CommandError("--package-version requires --source-public-id")

          queryset = InstalledPackage.objects.all()

          if source_public_id_raw is not None:
              source_public_id = _parse_uuid(source_public_id_raw, what="--source-public-id")
              queryset = queryset.filter(source_public_id=source_public_id)

          if package_type is not None:
              queryset = queryset.filter(package_type=package_type)

          if package_version is not None:
              queryset = queryset.filter(package_version=package_version)

          rows = queryset.order_by("package_type", "source_public_id", "package_version")

          for row in rows:
              self.stdout.write("Operation: status")
              self.stdout.write(f"PackageType: {row.package_type}")
              self.stdout.write(f"Source: {row.source_public_id}")
              self.stdout.write(f"Version: {row.package_version}")
              self.stdout.write(f"State: {row.state}")
  ```

  Add the dispatch line inside `handle`:

  ```python
          if options["action"] == "status":
              self._handle_status(options)
              return
  ```

- [ ] Run the focused test again and confirm GREEN:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py::test_status_with_no_args_lists_installed_packages_deterministically -q`

- [ ] Add the remaining `status` tests:

  ```python
  def test_status_filters_by_exact_canonical_tuple(settings, signed_package, capsys):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
      capsys.readouterr()

      call_command(
          "manage_node_package", "status",
          "--package-type", "learning_path",
          "--source-public-id", str(signed_package["source_public_id"]),
          "--package-version", str(signed_package["version"]),
      )

      out = capsys.readouterr().out
      assert f"Version: {signed_package['version']}" in out


  def test_status_unknown_identity_returns_no_rows(settings, capsys):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      call_command(
          "manage_node_package", "status",
          "--package-type", "learning_path",
          "--source-public-id", str(uuid.uuid4()),
          "--package-version", "1",
      )
      assert capsys.readouterr().out == ""


  def test_status_rejects_version_without_source(settings):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      with pytest.raises(CommandError):
          call_command("manage_node_package", "status", "--package-version", "1")


  def test_status_never_exposes_database_primary_key(settings, signed_package, capsys):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
      capsys.readouterr()

      call_command("manage_node_package", "status")

      out = capsys.readouterr().out
      assert "pk" not in out.lower()
      assert "InstalledPackage object" not in out
  ```

- [ ] Run: `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py -k status -q`
- [ ] `git diff --check`; inspect the diff.
- [ ] Commit:
  `git add linuxlab/sync/management/commands/manage_node_package.py linuxlab/sync/tests/test_manage_node_package.py && git commit -m "feat: add Node package status command"`

**Task 5 STOP conditions:** none expected beyond the standing spec-wide
stop conditions (Section 21 below); `InstalledPackage.objects.filter(...)`
is an ordinary read path, untouched by `InstalledPackageQuerySet`'s write
guards.

## Task 6: Node `activate` and `rollback` commands

**Files:**
- Modify: `linuxlab/sync/management/commands/manage_node_package.py`
- Modify: `linuxlab/sync/tests/test_manage_node_package.py`

**Interfaces:**
- Consumes: `linuxlab.sync.services.activate_package(package)`,
  `linuxlab.sync.services.rollback_package(package_type, source_public_id)`,
  `linuxlab.sync.services.PackageActivationError`,
  `linuxlab.sync.services.PackageRollbackError`.
- Produces: `manage.py manage_node_package activate --package-type <TYPE>
  --source-public-id <UUID> --package-version <VERSION>` and
  `manage.py manage_node_package rollback --package-type <TYPE>
  --source-public-id <UUID>`.

- [ ] Write the first failing test for `activate`:

  ```python
  def test_activate_transitions_staged_package_to_active(settings, signed_package, capsys):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
      capsys.readouterr()

      call_command(
          "manage_node_package", "activate",
          "--package-type", "learning_path",
          "--source-public-id", str(signed_package["source_public_id"]),
          "--package-version", str(signed_package["version"]),
      )

      out = capsys.readouterr().out
      assert "Operation: activate" in out
      assert "State: active" in out
  ```

- [ ] Run it and confirm RED:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py::test_activate_transitions_staged_package_to_active -q`
  — expected failure: `CommandError: unrecognized arguments` (the
  `activate` subparser does not exist yet).

- [ ] Implement `activate`. Add to `add_arguments`:

  ```python
          activate_parser = subparsers.add_parser("activate")
          activate_parser.add_argument("--package-type", required=True)
          activate_parser.add_argument("--source-public-id", required=True)
          activate_parser.add_argument("--package-version", required=True, type=int)
  ```

  Add the dispatch branch and handler:

  ```python
      def _handle_activate(self, options):
          from linuxlab.sync.models import InstalledPackage
          from linuxlab.sync.services import PackageActivationError, activate_package

          source_public_id = _parse_uuid(
              options["source_public_id"], what="--source-public-id"
          )

          try:
              package = InstalledPackage.objects.get(
                  package_type=options["package_type"],
                  source_public_id=source_public_id,
                  package_version=options["package_version"],
              )
          except InstalledPackage.DoesNotExist as exc:
              raise CommandError(
                  f"no installed artifact for {options['package_type']!r} / "
                  f"{source_public_id} / version {options['package_version']}"
              ) from exc

          try:
              activated = activate_package(package)
          except PackageActivationError as exc:
              raise CommandError(f"activation failed: {exc}") from exc

          self.stdout.write("Operation: activate")
          self.stdout.write(f"PackageType: {activated.package_type}")
          self.stdout.write(f"Source: {activated.source_public_id}")
          self.stdout.write(f"Version: {activated.package_version}")
          self.stdout.write(f"State: {activated.state}")
  ```

  Add the dispatch line inside `handle`:

  ```python
          if options["action"] == "activate":
              self._handle_activate(options)
              return
  ```

- [ ] Run the focused test again and confirm GREEN:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py::test_activate_transitions_staged_package_to_active -q`

- [ ] Add the remaining `activate` tests:

  ```python
  def test_activate_rejects_unknown_tuple(settings):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      with pytest.raises(CommandError):
          call_command(
              "manage_node_package", "activate",
              "--package-type", "learning_path",
              "--source-public-id", str(uuid.uuid4()),
              "--package-version", "1",
          )


  def test_activate_rejects_malformed_source_uuid(settings):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      with pytest.raises(CommandError):
          call_command(
              "manage_node_package", "activate",
              "--package-type", "learning_path",
              "--source-public-id", "not-a-uuid",
              "--package-version", "1",
          )


  def test_activate_fails_before_mutation_on_wrong_profile(settings, signed_package):
      from linuxlab.sync.models import ACTIVE, InstalledPackage

      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      call_command("manage_node_package", "import", "--file", str(signed_package["path"]))

      settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
      with pytest.raises(CommandError):
          call_command(
              "manage_node_package", "activate",
              "--package-type", "learning_path",
              "--source-public-id", str(signed_package["source_public_id"]),
              "--package-version", str(signed_package["version"]),
          )

      assert not InstalledPackage.objects.filter(state=ACTIVE).exists()


  def test_activate_maintains_single_active_invariant(settings, signed_package):
      from linuxlab.sync.models import ACTIVE, InstalledPackage

      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
      call_command(
          "manage_node_package", "activate",
          "--package-type", "learning_path",
          "--source-public-id", str(signed_package["source_public_id"]),
          "--package-version", str(signed_package["version"]),
      )

      active_count = InstalledPackage.objects.filter(
          package_type="learning_path",
          source_public_id=signed_package["source_public_id"],
          state=ACTIVE,
      ).count()
      assert active_count == 1
  ```

- [ ] Run: `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py -k activate -q`
- [ ] `git diff --check`; inspect the diff.
- [ ] Commit:
  `git add linuxlab/sync/management/commands/manage_node_package.py linuxlab/sync/tests/test_manage_node_package.py && git commit -m "feat: add Node package activate command"`

Now `rollback` — this needs a genuine second version to roll back from,
built and imported through the CLI itself (never a direct ORM insert):

- [ ] Add a second fixture, `second_signed_package`, that republishes the
  same `LearningPathPage` (reusing `signed_package`'s `path` and
  `private_key` so both versions share one trust anchor) and builds version
  2:

  ```python
  @pytest.fixture
  def rollback_world(signed_package, tmp_path):
      from linuxlab.activities.default_registry import build_default_activity_registry
      from linuxlab.content.models import LearningPathPage
      from linuxlab.publication.package import build_package
      from linuxlab.publication.snapshots import publish_learning_path

      page = LearningPathPage.objects.get(public_id=signed_package["source_public_id"])
      registry = build_default_activity_registry()
      snapshot_b = publish_learning_path(page, registry)
      package_bytes_b = build_package(snapshot_b, registry, signed_package["private_key"])

      package_path_b = tmp_path / "package-b.llpkg"
      package_path_b.write_bytes(package_bytes_b)

      return {**signed_package, "path_b": package_path_b, "version_b": snapshot_b.version}
  ```

- [ ] Write the first failing test:

  ```python
  def test_rollback_reactivates_previous_package(settings, rollback_world, capsys):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      world = rollback_world

      call_command("manage_node_package", "import", "--file", str(world["path"]))
      call_command(
          "manage_node_package", "activate",
          "--package-type", "learning_path",
          "--source-public-id", str(world["source_public_id"]),
          "--package-version", str(world["version"]),
      )
      call_command("manage_node_package", "import", "--file", str(world["path_b"]))
      call_command(
          "manage_node_package", "activate",
          "--package-type", "learning_path",
          "--source-public-id", str(world["source_public_id"]),
          "--package-version", str(world["version_b"]),
      )
      capsys.readouterr()

      call_command(
          "manage_node_package", "rollback",
          "--package-type", "learning_path",
          "--source-public-id", str(world["source_public_id"]),
      )

      out = capsys.readouterr().out
      assert f"Version: {world['version']}" in out
      assert "State: active" in out
  ```

- [ ] Run it and confirm RED:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py::test_rollback_reactivates_previous_package -q`
  — expected failure: `CommandError: unrecognized arguments` (the
  `rollback` subparser does not exist yet).

- [ ] Implement `rollback`. Add to `add_arguments`:

  ```python
          rollback_parser = subparsers.add_parser("rollback")
          rollback_parser.add_argument("--package-type", required=True)
          rollback_parser.add_argument("--source-public-id", required=True)
  ```

  Add the dispatch branch and handler:

  ```python
      def _handle_rollback(self, options):
          from linuxlab.sync.services import PackageRollbackError, rollback_package

          source_public_id = _parse_uuid(
              options["source_public_id"], what="--source-public-id"
          )

          try:
              rolled_back = rollback_package(options["package_type"], source_public_id)
          except PackageRollbackError as exc:
              raise CommandError(f"rollback failed: {exc}") from exc

          self.stdout.write("Operation: rollback")
          self.stdout.write(f"PackageType: {rolled_back.package_type}")
          self.stdout.write(f"Source: {rolled_back.source_public_id}")
          self.stdout.write(f"Version: {rolled_back.package_version}")
          self.stdout.write(f"State: {rolled_back.state}")
  ```

  Add the dispatch line inside `handle`:

  ```python
          if options["action"] == "rollback":
              self._handle_rollback(options)
              return
  ```

- [ ] Run the focused test again and confirm GREEN:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py::test_rollback_reactivates_previous_package -q`

- [ ] Add the remaining `rollback` tests:

  ```python
  def test_rollback_fails_with_no_active_package(settings):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      with pytest.raises(CommandError):
          call_command(
              "manage_node_package", "rollback",
              "--package-type", "learning_path",
              "--source-public-id", str(uuid.uuid4()),
          )


  def test_rollback_fails_when_active_has_no_previous_package(settings, signed_package):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      call_command("manage_node_package", "import", "--file", str(signed_package["path"]))
      call_command(
          "manage_node_package", "activate",
          "--package-type", "learning_path",
          "--source-public-id", str(signed_package["source_public_id"]),
          "--package-version", str(signed_package["version"]),
      )

      with pytest.raises(CommandError):
          call_command(
              "manage_node_package", "rollback",
              "--package-type", "learning_path",
              "--source-public-id", str(signed_package["source_public_id"]),
          )


  def test_rollback_rejects_malformed_source_uuid(settings):
      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      with pytest.raises(CommandError):
          call_command(
              "manage_node_package", "rollback",
              "--package-type", "learning_path", "--source-public-id", "not-a-uuid",
          )


  def test_rollback_fails_before_mutation_on_wrong_profile(settings, rollback_world):
      from linuxlab.sync.models import ACTIVE, InstalledPackage

      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      world = rollback_world
      call_command("manage_node_package", "import", "--file", str(world["path"]))
      call_command(
          "manage_node_package", "activate",
          "--package-type", "learning_path",
          "--source-public-id", str(world["source_public_id"]),
          "--package-version", str(world["version"]),
      )
      call_command("manage_node_package", "import", "--file", str(world["path_b"]))
      call_command(
          "manage_node_package", "activate",
          "--package-type", "learning_path",
          "--source-public-id", str(world["source_public_id"]),
          "--package-version", str(world["version_b"]),
      )

      settings.LINUXLAB_CAN_IMPORT_PACKAGES = False
      with pytest.raises(CommandError):
          call_command(
              "manage_node_package", "rollback",
              "--package-type", "learning_path",
              "--source-public-id", str(world["source_public_id"]),
          )

      active = InstalledPackage.objects.get(
          package_type="learning_path", source_public_id=world["source_public_id"], state=ACTIVE
      )
      assert active.package_version == world["version_b"]


  def test_rollback_maintains_single_active_invariant(settings, rollback_world):
      from linuxlab.sync.models import ACTIVE, InstalledPackage

      settings.LINUXLAB_CAN_IMPORT_PACKAGES = True
      world = rollback_world
      call_command("manage_node_package", "import", "--file", str(world["path"]))
      call_command(
          "manage_node_package", "activate",
          "--package-type", "learning_path",
          "--source-public-id", str(world["source_public_id"]),
          "--package-version", str(world["version"]),
      )
      call_command("manage_node_package", "import", "--file", str(world["path_b"]))
      call_command(
          "manage_node_package", "activate",
          "--package-type", "learning_path",
          "--source-public-id", str(world["source_public_id"]),
          "--package-version", str(world["version_b"]),
      )
      call_command(
          "manage_node_package", "rollback",
          "--package-type", "learning_path",
          "--source-public-id", str(world["source_public_id"]),
      )

      active_count = InstalledPackage.objects.filter(
          package_type="learning_path",
          source_public_id=world["source_public_id"],
          state=ACTIVE,
      ).count()
      assert active_count == 1
  ```

- [ ] Run the full command test suite:
  `.venv/bin/pytest linuxlab/sync/tests/test_manage_node_package.py -q`
- [ ] Run the sync app regression suite:
  `.venv/bin/pytest linuxlab/sync/tests/ -q`
- [ ] `git diff --check`; inspect the diff.
- [ ] Commit:
  `git add linuxlab/sync/management/commands/manage_node_package.py linuxlab/sync/tests/test_manage_node_package.py && git commit -m "feat: add Node package rollback command"`

**Task 6 STOP conditions:** STOP and report if `activate_package`'s
already-ACTIVE-is-a-no-op behavior, or `rollback_package`'s
`previous_package` selection, is found (upon writing these tests against
real code) to differ from the description in spec Section 9 — this plan's
tests assert the described behavior; if the real service disagrees, that
is a live mismatch requiring architecture review, not a test adjusted to
match unexpected behavior.

## Task 7: CLI-driven Central→file→Node end-to-end acceptance test

**Files:**
- Create: `scripts/tests/_package_operator_subprocess.py` (test-only
  subprocess bootstrap; never imported by production code)
- Create: `scripts/tests/test_package_operator_e2e.py`

**Interfaces:**
- Consumes (reused as generic test/database orchestration —
  category A, per the inventory below):
  `scripts.release_gate.database.GateDatabaseNames.for_run(run_id)`,
  `create_database(db_admin, gate_names, member)`,
  `run_migrations(gate_names, member, profile_env)`,
  `drop_database(db_admin, gate_names, member)`,
  `scripts.release_gate.environment.DatabaseAdminConnection`.
- Explicitly NOT consumed (release-gate-only — category B, never a
  production dependency and never imported by this test either):
  `scripts.release_gate.signing_material` (ephemeral keypair generation),
  `scripts.release_gate.actions`, `scripts.release_gate.gate_runner`,
  `scripts.release_gate.cli`'s action dispatch,
  `scripts.release_gate.environment.build_gate_environment` (its
  git-sha/clean-working-tree/artifact-dir concerns are release-gate-run
  concepts this test does not need).
- Produces: two new test-only files; no new production code, no shared
  production module. `manage_central_package`/`manage_node_package`
  themselves are exercised completely unmodified, exactly as built in
  Tasks 2–6.

### Existing DB/profile test infrastructure inventory (read before writing this task)

`scripts/release_gate/database.py` already provides hardened, reusable
ephemeral-PostgreSQL orchestration:

- `GateDatabaseNames.for_run(run_id)` derives four canonical, safety-checked
  database names (`linuxlab_gate_{central,node,restore_central,restore_node}_{run_id}`)
  from one `run_id` matching `^[a-z][a-z0-9_]*\Z`
  (`scripts/release_gate/environment.py`'s `_RUN_ID_PATTERN`); every
  create/migrate/drop call below re-derives and checks this name, so a
  forged/foreign/production-looking name can never be operated on
  (`assert_ephemeral_database_name`).
- `create_database(db_admin, gate_names, member)` issues `CREATE DATABASE`
  for one of the four names.
- `run_migrations(gate_names, member, profile_env, *, repo_root=None)` runs
  `manage.py migrate --noinput` as a **subprocess**, with
  `POSTGRES_DB` pinned to the resolved ephemeral name last (so nothing in
  `profile_env` can override it) — this is the exact established pattern
  this task reuses for running `manage_central_package`/
  `manage_node_package` too.
- `drop_database(db_admin, gate_names, member, ...)` is idempotent,
  privilege-safe cleanup: it polls `pg_stat_activity` for the database's
  remaining connections (bounded by a deadline), then issues a plain
  `DROP DATABASE IF EXISTS`, then verifies the database is actually gone —
  it never calls `pg_terminate_backend()`. Dropping a name that was never
  created is a safe no-op.
- `scripts/release_gate/environment.py`'s `DatabaseAdminConnection` is a
  plain four-field dataclass (`host`/`port`/`user`/`password`) read from
  `POSTGRES_HOST`/`POSTGRES_PORT`/`POSTGRES_USER`/`POSTGRES_PASSWORD` — the
  same environment variables `.env` already provides for local Postgres
  (per CLAUDE.md).

Explicitly NOT reused, because it is release-gate-run business logic, not
generic database orchestration:
`build_gate_environment()` (asserts a clean git working tree and computes
a git SHA and an `artifacts/phase1-release/<run_id>` directory — none of
which this test needs or wants); `scripts/release_gate/signing_material.py`
(`create_ephemeral_signing_keypair` — its own docstring calls it "ephemeral
Ed25519 signing keypair lifecycle," test/release-gate scaffolding only);
`scripts/release_gate/dual_deployment` in `e2e_support.py` (spins up real
Gunicorn HTTP servers for full deployment-level scenarios — heavier than
this test needs, and it predates Amendment C, so it has no mechanism to
redirect a Node process's trust-anchor path without touching the real
`/etc/linuxlab` path); `scripts/release_gate/cli.py`'s action dispatch and
`scripts/release_gate/actions.py` (call the same domain services this
plan's own commands call, but through release-gate-specific wrapper
functions this test has no reason to go through when the real commands
under test are `manage_central_package`/`manage_node_package` themselves).

### Subprocess boundary and trust-material strategy (concrete decision, not left open)

Central and Node run in **separate OS subprocesses**, one process per
management-command invocation, each given only its own profile's
`DJANGO_SETTINGS_MODULE` and `POSTGRES_DB` via that subprocess's own
`env=`, exactly mirroring `run_migrations()`'s already-established
pattern above. No `override_settings(...)` and no mutable global
`settings` fixture is used anywhere in this task — Django's settings/app
registry is configured exactly once, at each subprocess's own startup,
and that subprocess exits after one command.

Because each profile runs in its own fresh interpreter, `unittest.mock`/
pytest's `monkeypatch` (which only work within one already-running
process) cannot reach across the subprocess boundary. Instead, a small
test-only bootstrap script, `scripts/tests/_package_operator_subprocess.py`,
is the literal target of each subprocess invocation: it calls
`django.setup()` once, optionally reassigns
`linuxlab.publication.signing._SIGNING_KEY_PATH` or
`linuxlab.sync.trust._TRUST_ANCHOR_PATH` to a test-generated file path
**inside that fresh process's own module state** (a plain attribute
assignment on an already-imported module — not a mock, not needed to be
"undone," since the process exits immediately after), and then calls the
real, completely unmodified `manage_central_package`/`manage_node_package`
command via `django.core.management.call_command()`. This is test-only
code, analogous in spirit to `scripts/release_gate/cli.py` being a
standalone non-production subprocess entry point, but far smaller and
specific to this one test — it is never imported by
`linuxlab/publication/` or `linuxlab/sync/` production code, and it never
writes to the real `/etc/linuxlab/keys/` path. Production loader/command
code (Tasks 1–6) is not modified by this task in any way.

The Central-flavored subprocess request carries `signing_key_path`
(pointing at a `tmp_path` file); the Node-flavored request carries
`trust_anchor_path` (pointing at a **different** `tmp_path` file
containing only the matching *public* key). No request ever carries both,
and the Central private key bytes are never written anywhere the
Node-flavored subprocess call can read them — only `trust_anchor_path`'s
public-key file is available to Node.

- [ ] Create `scripts/tests/_package_operator_subprocess.py`:

  ```python
  """Test-only subprocess bootstrap for
  scripts/tests/test_package_operator_e2e.py. Never imported by
  production code. Runs `django.setup()` once, optionally points the
  Central signing-key loader or Node trust-anchor loader at a
  test-generated file (a plain module-attribute assignment, valid only
  for this one short-lived process), then either bootstraps one
  LearningPathPage or calls one real management command unmodified.
  """

  import json
  import sys
  from pathlib import Path

  import django

  django.setup()

  from django.core.management import call_command  # noqa: E402
  from django.core.management.base import CommandError  # noqa: E402


  def _bootstrap_learning_path(request):
      from linuxlab.content.models import LearningPathPage
      from linuxlab.publication.tests._wagtail_bootstrap import get_or_create_root_page

      root = get_or_create_root_page()
      page = LearningPathPage(title=request["title"], slug=request["slug"])
      root.add_child(instance=page)
      print(f"LEARNING_PATH_PUBLIC_ID:{page.public_id}")


  def main():
      request = json.loads(sys.argv[1])

      if request.get("signing_key_path"):
          import linuxlab.publication.signing as signing_module

          signing_module._SIGNING_KEY_PATH = Path(request["signing_key_path"])

      if request.get("trust_anchor_path"):
          import linuxlab.sync.trust as trust_module

          trust_module._TRUST_ANCHOR_PATH = Path(request["trust_anchor_path"])

      if request["mode"] == "bootstrap_learning_path":
          _bootstrap_learning_path(request)
          return

      try:
          call_command(*request["command"])
      except CommandError as exc:
          print(f"COMMAND_ERROR: {exc}", file=sys.stderr)
          sys.exit(1)


  if __name__ == "__main__":
      main()
  ```

  This file is test-only infrastructure, not a test itself (no `test_`
  prefix; mirrors the already-established `linuxlab/publication/tests/
  _wagtail_bootstrap.py` naming convention for non-test helper files
  inside a `tests/` directory).

- [ ] Write the complete E2E test in `scripts/tests/test_package_operator_e2e.py`
  (this is Task 7's only RED→GREEN cycle, since it composes commands
  already proven individually in Tasks 2–6 — RED is expected only if any
  prior task's command has a defect that only surfaces under this full
  two-database sequence):

  ```python
  import json
  import os
  import secrets
  import subprocess
  import sys
  import uuid
  from pathlib import Path

  import psycopg
  from psycopg import sql
  import pytest

  from scripts.release_gate.database import (
      GateDatabaseNames,
      create_database,
      drop_database,
      run_migrations,
  )
  from scripts.release_gate.environment import DatabaseAdminConnection

  _REPO_ROOT = Path(__file__).resolve().parents[2]
  _SUBPROCESS_SCRIPT = Path(__file__).parent / "_package_operator_subprocess.py"


  def _db_admin_from_env():
      return DatabaseAdminConnection(
          host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
          port=os.environ.get("POSTGRES_PORT", "5432"),
          user=os.environ.get("POSTGRES_USER", "linuxlab"),
          password=os.environ.get("POSTGRES_PASSWORD", ""),
      )


  def _run_subprocess_command(*, settings_module, db_name, request):
      child_env = {
          **os.environ,
          "DJANGO_SETTINGS_MODULE": settings_module,
          "POSTGRES_DB": db_name,
      }
      return subprocess.run(
          [sys.executable, str(_SUBPROCESS_SCRIPT), json.dumps(request)],
          cwd=str(_REPO_ROOT),
          env=child_env,
          capture_output=True,
          text=True,
      )


  def _count_rows(db_admin, db_name, table_name):
      conn = psycopg.connect(
          host=db_admin.host, port=db_admin.port, user=db_admin.user,
          password=db_admin.password, dbname=db_name,
      )
      try:
          with conn.cursor() as cursor:
              cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table_name)))
              return cursor.fetchone()[0]
      finally:
          conn.close()


  @pytest.fixture
  def package_operator_e2e_databases():
      db_admin = _db_admin_from_env()
      run_id = f"pkgope2e{secrets.token_hex(4)}"
      gate_names = GateDatabaseNames.for_run(run_id)

      create_database(db_admin, gate_names, "central")
      run_migrations(
          gate_names, "central", {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.central"}
      )

      create_database(db_admin, gate_names, "node")
      run_migrations(gate_names, "node", {"DJANGO_SETTINGS_MODULE": "linuxlab.settings.node"})

      try:
          yield {"db_admin": db_admin, "gate_names": gate_names}
      finally:
          # Cleanup runs on pass AND failure (this is inside `finally`),
          # and both databases are attempted even if one drop fails, so a
          # Node cleanup failure never skips Central cleanup (matching
          # database.py's own documented cleanup invariant).
          errors = []
          for member in ("central", "node"):
              try:
                  drop_database(db_admin, gate_names, member)
              except Exception as exc:  # noqa: BLE001
                  errors.append(exc)
          if errors:
              raise RuntimeError(f"ephemeral database cleanup failed: {errors}")


  def test_full_operator_workflow_central_to_node(package_operator_e2e_databases, tmp_path):
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      from cryptography.hazmat.primitives.serialization import (
          Encoding, NoEncryption, PrivateFormat, PublicFormat,
      )

      db_admin = package_operator_e2e_databases["db_admin"]
      gate_names = package_operator_e2e_databases["gate_names"]
      central_db = gate_names.central
      node_db = gate_names.node

      # --- fixture/bootstrap: test-only trust material, written only
      # under tmp_path (cleaned up by pytest itself) -- never the real
      # /etc/linuxlab path, and the private key file is never referenced
      # by any Node-flavored request below. ---
      private_key = Ed25519PrivateKey.generate()
      signing_key_path = tmp_path / "package-signing.key"
      signing_key_path.write_bytes(
          private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
      )
      trust_anchor_path = tmp_path / "package-signing-public.key"
      trust_anchor_path.write_bytes(
          private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
      )

      # --- database isolation proof, BEFORE any Central or Node command
      # runs: the freshly migrated Node database has zero rows in either
      # a Central-owned table or its own lifecycle table. ---
      assert _count_rows(db_admin, node_db, "publication_publicationsnapshot") == 0
      assert _count_rows(db_admin, node_db, "sync_installedpackage") == 0

      # --- Central: bootstrap one LearningPathPage directly in the
      # Central database (authoring fixture only, not a lifecycle step
      # this test proves via CLI). ---
      bootstrap = _run_subprocess_command(
          settings_module="linuxlab.settings.central", db_name=central_db,
          request={
              "mode": "bootstrap_learning_path",
              "title": "E2E Path",
              "slug": f"e2e-{uuid.uuid4().hex[:8]}",
          },
      )
      assert bootstrap.returncode == 0, bootstrap.stderr
      learning_path_id = bootstrap.stdout.strip().removeprefix("LEARNING_PATH_PUBLIC_ID:")

      # --- Central: publish + build version A, through the CLI only ---
      publish_a = _run_subprocess_command(
          settings_module="linuxlab.settings.central", db_name=central_db,
          request={
              "mode": "command",
              "signing_key_path": str(signing_key_path),
              "command": [
                  "manage_central_package", "publish", "--learning-path", learning_path_id
              ],
          },
      )
      assert publish_a.returncode == 0, publish_a.stderr
      assert "Version: 1" in publish_a.stdout

      package_a_path = tmp_path / "package-a.llpkg"
      build_a = _run_subprocess_command(
          settings_module="linuxlab.settings.central", db_name=central_db,
          request={
              "mode": "command",
              "signing_key_path": str(signing_key_path),
              "command": [
                  "manage_central_package", "build",
                  "--learning-path", learning_path_id, "--version", "1",
                  "--output", str(package_a_path),
              ],
          },
      )
      assert build_a.returncode == 0, build_a.stderr
      assert package_a_path.exists()

      # --- pure file boundary: from here on, only package_a_path's bytes
      # reach Node. Every Node-flavored request below omits central_db and
      # signing_key_path entirely; it is not merely unused, it is never
      # constructed. ---

      # --- Node: verify (read-only) ---
      before = _count_rows(db_admin, node_db, "sync_installedpackage")
      verify_a = _run_subprocess_command(
          settings_module="linuxlab.settings.node", db_name=node_db,
          request={
              "mode": "command",
              "trust_anchor_path": str(trust_anchor_path),
              "command": ["manage_node_package", "verify", "--file", str(package_a_path)],
          },
      )
      assert verify_a.returncode == 0, verify_a.stderr
      assert _count_rows(db_admin, node_db, "sync_installedpackage") == before

      # --- Node: import, status, activate ---
      import_a = _run_subprocess_command(
          settings_module="linuxlab.settings.node", db_name=node_db,
          request={
              "mode": "command",
              "trust_anchor_path": str(trust_anchor_path),
              "command": ["manage_node_package", "import", "--file", str(package_a_path)],
          },
      )
      assert import_a.returncode == 0, import_a.stderr

      status_after_import = _run_subprocess_command(
          settings_module="linuxlab.settings.node", db_name=node_db,
          request={"mode": "command", "command": ["manage_node_package", "status"]},
      )
      assert status_after_import.returncode == 0, status_after_import.stderr
      assert "State: staged" in status_after_import.stdout

      activate_a = _run_subprocess_command(
          settings_module="linuxlab.settings.node", db_name=node_db,
          request={
              "mode": "command",
              "command": [
                  "manage_node_package", "activate",
                  "--package-type", "learning_path",
                  "--source-public-id", learning_path_id,
                  "--package-version", "1",
              ],
          },
      )
      assert activate_a.returncode == 0, activate_a.stderr
      assert "State: active" in activate_a.stdout

      # --- Central: version B, through the same operator workflow ---
      publish_b = _run_subprocess_command(
          settings_module="linuxlab.settings.central", db_name=central_db,
          request={
              "mode": "command",
              "signing_key_path": str(signing_key_path),
              "command": [
                  "manage_central_package", "publish", "--learning-path", learning_path_id
              ],
          },
      )
      assert publish_b.returncode == 0, publish_b.stderr
      assert "Version: 2" in publish_b.stdout

      package_b_path = tmp_path / "package-b.llpkg"
      build_b = _run_subprocess_command(
          settings_module="linuxlab.settings.central", db_name=central_db,
          request={
              "mode": "command",
              "signing_key_path": str(signing_key_path),
              "command": [
                  "manage_central_package", "build",
                  "--learning-path", learning_path_id, "--version", "2",
                  "--output", str(package_b_path),
              ],
          },
      )
      assert build_b.returncode == 0, build_b.stderr

      # --- Node: version B ---
      verify_b = _run_subprocess_command(
          settings_module="linuxlab.settings.node", db_name=node_db,
          request={
              "mode": "command",
              "trust_anchor_path": str(trust_anchor_path),
              "command": ["manage_node_package", "verify", "--file", str(package_b_path)],
          },
      )
      assert verify_b.returncode == 0, verify_b.stderr

      import_b = _run_subprocess_command(
          settings_module="linuxlab.settings.node", db_name=node_db,
          request={
              "mode": "command",
              "trust_anchor_path": str(trust_anchor_path),
              "command": ["manage_node_package", "import", "--file", str(package_b_path)],
          },
      )
      assert import_b.returncode == 0, import_b.stderr

      activate_b = _run_subprocess_command(
          settings_module="linuxlab.settings.node", db_name=node_db,
          request={
              "mode": "command",
              "command": [
                  "manage_node_package", "activate",
                  "--package-type", "learning_path",
                  "--source-public-id", learning_path_id,
                  "--package-version", "2",
              ],
          },
      )
      assert activate_b.returncode == 0, activate_b.stderr
      assert "State: active" in activate_b.stdout

      # --- Node: rollback to the existing lifecycle's own previous_package ---
      rollback = _run_subprocess_command(
          settings_module="linuxlab.settings.node", db_name=node_db,
          request={
              "mode": "command",
              "command": [
                  "manage_node_package", "rollback",
                  "--package-type", "learning_path",
                  "--source-public-id", learning_path_id,
              ],
          },
      )
      assert rollback.returncode == 0, rollback.stderr
      assert "Version: 1" in rollback.stdout
      assert "State: active" in rollback.stdout

      status_final = _run_subprocess_command(
          settings_module="linuxlab.settings.node", db_name=node_db,
          request={"mode": "command", "command": ["manage_node_package", "status"]},
      )
      assert status_final.stdout.count("State: active") == 1
  ```

- [ ] Run it:
  `env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest scripts/tests/test_package_operator_e2e.py -q`
  (unset, not left as whatever the shell happens to export, exactly like
  the rest of the `scripts/tests` suite — this test configures
  `DJANGO_SETTINGS_MODULE` itself, per subprocess, via `env=`) — this must
  reach GREEN using only the commands built in Tasks 1–6; do not add any
  new command logic in this task. If it fails, the defect is in an earlier
  task's command, not in this test, and must be fixed by revisiting that
  task (do not weaken this test's assertions to accommodate a defect).
- [ ] `git diff --check`; inspect the diff — confirm it touches only these
  two new test-infrastructure files.
- [ ] Commit:
  `git add scripts/tests/_package_operator_subprocess.py scripts/tests/test_package_operator_e2e.py && git commit -m "test: verify package operator workflow end to end"`

**Task 7 STOP conditions:** STOP and report if `GateDatabaseNames.for_run()`
combined with `create_database`/`run_migrations`/`drop_database` cannot
actually produce two working, separately migrated databases outside a
full release-gate run (e.g. if `run_migrations()`'s subprocess call is
found to depend on release-gate-run-only environment state this test does
not set up) — re-verify by running the fixture alone before writing the
rest of the test, and escalate rather than papering over it with a new,
uninventoried helper; if the real Ed25519/PEM round-trip through the
subprocess bootstrap script behaves differently from the in-process
Task 1 loader tests (it should not — the loader functions themselves are
completely unmodified), that is a genuine defect in Task 1, not something
to work around here.

## Task 8: Final verification at the exact implementation HEAD

**Files:** none (verification only).

- [ ] Confirm the working tree is clean and record the exact HEAD:
  `git status --short && git rev-parse HEAD`

- [ ] Source local Postgres credentials (never print them):

  ```bash
  set -a
  source .env
  set +a
  ```

- [ ] New package-operator focused tests:
  ```
  .venv/bin/pytest \
    linuxlab/sync/tests/test_trust.py \
    linuxlab/publication/tests/test_signing.py \
    linuxlab/publication/tests/test_manage_central_package.py \
    linuxlab/sync/tests/test_manage_node_package.py \
    -q
  ```

- [ ] New package-operator CLI-driven E2E test (its own invocation, since
  it lives under `scripts/tests/` and manages its own
  `DJANGO_SETTINGS_MODULE`/`POSTGRES_DB` per subprocess — do not fold it
  into the `linuxlab/**/tests` invocation above):
  ```
  env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest scripts/tests/test_package_operator_e2e.py -q
  ```

- [ ] Relevant existing publication/package tests:
  ```
  .venv/bin/pytest \
    linuxlab/publication/tests/test_snapshots.py \
    linuxlab/publication/tests/test_publish_learning_path.py \
    linuxlab/publication/tests/test_package.py \
    linuxlab/publication/tests/test_concurrency.py \
    linuxlab/publication/tests/test_models.py \
    linuxlab/publication/tests/test_validators.py \
    linuxlab/publication/tests/test_integration.py \
    -q
  ```

- [ ] Relevant existing Node/InstalledPackage lifecycle tests:
  ```
  .venv/bin/pytest \
    linuxlab/sync/tests/test_import_package.py \
    linuxlab/sync/tests/test_activation.py \
    linuxlab/sync/tests/test_rollback.py \
    linuxlab/sync/tests/test_models.py \
    -q
  ```

- [ ] Relevant runtime/sync regression tests:
  ```
  .venv/bin/pytest \
    linuxlab/runtime/tests/ \
    linuxlab/sync/tests/test_task14_command.py \
    linuxlab/sync/tests/test_task14_profiles.py \
    linuxlab/sync/tests/test_offline.py \
    linuxlab/sync/tests/test_views.py \
    -q
  ```

- [ ] Scripts suite:
  ```
  env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest scripts/tests -q
  ```

- [ ] FULL repository suite (mandatory):
  ```
  env -u DJANGO_SETTINGS_MODULE .venv/bin/pytest -q
  ```
  Require 0 failures, 0 unexpected skips.

- [ ] Ruff:
  ```
  .venv/bin/ruff check .
  ```

- [ ] Central check:
  ```
  DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py check
  ```

- [ ] Node check:
  ```
  DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py check
  ```

- [ ] Central migration drift:
  ```
  DJANGO_SETTINGS_MODULE=linuxlab.settings.central .venv/bin/python manage.py makemigrations --check --dry-run
  ```

- [ ] Node migration drift:
  ```
  DJANGO_SETTINGS_MODULE=linuxlab.settings.node .venv/bin/python manage.py makemigrations --check --dry-run
  ```
  Both must report no drift — this corrective introduces no model changes,
  so no new migration file should be proposed.

- [ ] Confirm the working tree is clean after all commits:
  `git status --short`

- [ ] Self-review the full diff across all Task 1–7 commits against the
  approved spec and the Independent Review Checkpoint below before
  reporting this task complete.

**Task 8 STOP conditions:** any unexpected test failure, any migration
drift, or any Ruff finding must be fixed (or, if it reveals a genuine
architecture mismatch rather than a local bug, reported and escalated) — do
not report completion with a known-red suite.

## Independent Review Checkpoint

The coordinator/reviewer should verify each of the following once
implementation is complete:

1. Are the management commands truly thin adapters (no reimplemented
   domain logic, no new business rules)?
2. Is Central publication identity exact and non-synthetic
   (`--learning-path` + `--version`, never a fabricated `--publication`
   UUID)?
3. Is Node installed identity exact and non-synthetic (the composite
   `(package_type, source_public_id, package_version)` tuple, never a
   fabricated UUID)?
4. Is the signing private key loaded only from
   `/etc/linuxlab/keys/package-signing.key` via
   `load_package_signing_private_key()`, with no CLI/env/settings
   override?
5. Is the Node trust anchor loaded only from
   `/etc/linuxlab/keys/package-signing-public.key` via
   `load_package_trusted_public_key()`, with no CLI/env/settings override?
6. Can package content influence which trust key is loaded? (It must not —
   the loader never inspects the package.)
7. Is release-gate ephemeral trust material (`scripts/release_gate/`)
   absent from every production code path added by this plan?
8. Does wrong capability/profile fail before key loading and before any
   mutation, in every command that mutates state?
9. Does `build` select the exact requested `(source_public_id, version)`
   snapshot, never "latest"?
10. Is `build`'s file output fail-closed, atomic, and non-overwriting (via
    `os.link`, never a bare `os.replace` on an existence check)?
11. Is `verify` read-only (zero `InstalledPackage` mutation on success or
    failure)?
12. Does `import` independently re-verify rather than trusting a prior
    `verify` call (proven by the adversarial tamper-after-verify test)?
13. Does a structurally valid but wrong Ed25519 key fail signature
    validation rather than being treated as a configuration error?
14. Does `activate` go through the existing lifecycle lock
    (`activate_package`), never a direct field write?
15. Does `rollback` call exactly `rollback_package(package_type,
    source_public_id)`, with no explicit-target argument?
16. Does the single-ACTIVE-per-source invariant hold after every
    activate/rollback test?
17. Does the E2E test use two independently created and migrated
    ephemeral PostgreSQL databases (never a single shared test database),
    run Central and Node in separate subprocesses under their own real
    settings modules, and cross the Central→Node boundary only through
    the `.llpkg` file's bytes — proven by the explicit zero-row assertions
    against Node's database before any Node command runs?
18. Is there no schema migration (confirmed by the migration-drift checks
    in Task 8)?
19. Is `docs/operator/` unchanged?
20. Is Task 14 (`linuxlab/sync/services.py`'s outbox/inbox machinery,
    `sync_events`, `manage_sync_node`) unchanged?

Corrective A closes only with no valid Critical or Important finding
against this checklist.

## Spec Coverage Matrix

| Spec requirement | Implementation task | Verification |
|---|---|---|
| Thin adapters only, two commands (`manage_central_package`, `manage_node_package`) | Tasks 2–6 | Task 8 full suite + Independent Review Checkpoint #1 |
| Profile boundary: Central allowed/Node rejected and vice versa, before mutation | Tasks 2, 4, 6 (`_require_central_capability`/`_require_node_capability`) | wrong-profile tests in Tasks 2, 3, 4, 6; Checkpoint #8 |
| No new auth model / capability framework | Task 2, 4 (inline `getattr(settings, ...)`, no shared module) | Plan self-review scope check |
| **Amendment A** — `InstalledPackage` identity `(package_type, source_public_id, package_version)`, no `public_id` | Tasks 4–6 (all commands use the tuple; no UUID field referenced) | Checkpoint #3; status/activate/rollback tests |
| **Amendment A** — `activate_package(package)` via read-only tuple lookup | Task 6 `_handle_activate` | activation tests |
| **Amendment A** — `rollback_package(package_type, source_public_id)`, no explicit target | Task 6 `_handle_rollback` | rollback tests, Checkpoint #15 |
| **Amendment B** — `PublicationSnapshot` identity `(kind, source_public_id, version)`, no `public_id` | Tasks 2–3 (`--learning-path`/`--version`, no `--publication`) | Checkpoint #2; build tests |
| **Amendment B** — `kind` fixed to `"learning_path"`, implicit in CLI | Tasks 2–3 | publish/build output assertions |
| **Amendment B** — `build_package(snapshot, ...)` via read-only snapshot lookup | Task 3 `_handle_build` | build tests |
| **Amendment C** — fixed Node trust-anchor path, PEM Ed25519, no override | Task 1a (`linuxlab/sync/trust.py`) | loader tests; Checkpoint #5, #6 |
| **Amendment C** — Central signing-key loader, fixed path, no override | Task 1b (`linuxlab/publication/signing.py`) | loader tests; Checkpoint #4 |
| **Amendment C** — no trust store, no multi-key, no TOFU, no remote fetch, no release-gate fallback | Task 1 (module design excludes all of these) | Checkpoint #6, #7 |
| Node `verify` is read-only | Task 4 `_handle_verify` | zero-mutation tests; Checkpoint #11 |
| Node `import` independently re-verifies | Task 4 `_handle_import` | adversarial tamper-after-verify test; Checkpoint #12 |
| Node `status` deterministic discovery, canonical filters only | Task 5 | status tests |
| Safe, non-overwriting, atomic build output | Task 3a (`_write_new_file_atomically`) | helper tests; Checkpoint #10 |
| No secret material in output/logs | Tasks 1, 3, 4 (sentinel-absence tests) | Task 1a/1b sentinel tests |
| CLI-driven E2E through management commands only, file-only Central→Node boundary, real Central/Node DB separation | Task 7 (two ephemeral databases, subprocess-per-profile, zero-row assertions on Node before import) | E2E test; Checkpoint #17 |
| No schema migration | All tasks (no model changes anywhere in this plan) | Task 8 migration-drift checks; Checkpoint #18 |
| `docs/operator/` untouched | This plan does not touch it | Task 8 diff review; Checkpoint #19 |
| Task 14 unchanged | This plan does not touch `sync_events`/`manage_sync_node`/outbox/inbox code | Task 8 regression run; Checkpoint #20 |
| Corrective B / Corrective C scope (readiness consistency, docs) | Not started by this plan | Plan self-review scope check |

No active spec requirement is left uncovered; every Corrective C item the
spec explicitly defers (operator documentation of provisioning/rotation/
backup procedures) is DOCUMENTATION/OPERATIONAL ONLY and is not implemented
by this plan, per spec Sections 15–22 (Amendment C) and Section 25.
