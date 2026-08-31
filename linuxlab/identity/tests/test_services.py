"""T15-D27 corrective (identity-boundary finding): guest Learner
resolution/creation is an identity-owned concern (ADR-006/ADR-012),
not something sync.views may perform directly against the Learner
model."""

import uuid

import pytest

from linuxlab.identity.models import Account, Learner
from linuxlab.identity.services import (
    ensure_learner,
    resolve_existing_guest_learner,
    resolve_existing_learner_for_account,
    resolve_or_create_guest_learner,
)


@pytest.mark.django_db
def test_resolve_or_create_guest_learner_with_no_public_id_mints_a_fresh_guest():
    learner = resolve_or_create_guest_learner(public_id=None)

    assert learner.account_id is None
    assert isinstance(learner.public_id, uuid.UUID)


@pytest.mark.django_db
def test_resolve_or_create_guest_learner_reuses_an_existing_guest_by_public_id():
    first = resolve_or_create_guest_learner(public_id=None)

    second = resolve_or_create_guest_learner(public_id=first.public_id)

    assert second.pk == first.pk


@pytest.mark.django_db
def test_resolve_or_create_guest_learner_mints_fresh_when_public_id_does_not_resolve():
    learner = resolve_or_create_guest_learner(public_id=uuid.uuid4())

    assert learner.account_id is None


@pytest.mark.django_db
def test_resolve_or_create_guest_learner_never_returns_an_account_backed_learner():
    """An arbitrary/forged public_id must never let a caller silently
    attach to someone else's Account-backed Learner: only a genuine
    guest (account=None) row is ever reused."""
    account = Account.objects.create_user(username="alice")
    account_learner = ensure_learner(account)

    learner = resolve_or_create_guest_learner(public_id=account_learner.public_id)

    assert learner.pk != account_learner.pk
    assert learner.account_id is None


@pytest.mark.django_db
def test_resolve_or_create_guest_learner_is_idempotent_for_repeated_calls_with_the_same_id():
    first = resolve_or_create_guest_learner(public_id=None)

    second = resolve_or_create_guest_learner(public_id=first.public_id)
    third = resolve_or_create_guest_learner(public_id=second.public_id)

    assert Learner.objects.filter(public_id=first.public_id).count() == 1
    assert third.pk == first.pk


# --- read-only resolution for authorizing an EXISTING Attempt -------------
# (T15-D27 final corrective, read-only-authorization finding: must never
# create a Learner as a side effect of a denied request)


@pytest.mark.django_db
def test_resolve_existing_guest_learner_returns_none_for_unknown_public_id():
    learner = resolve_existing_guest_learner(public_id=uuid.uuid4())

    assert learner is None
    assert Learner.objects.count() == 0


@pytest.mark.django_db
def test_resolve_existing_guest_learner_returns_none_for_missing_public_id():
    learner = resolve_existing_guest_learner(public_id=None)

    assert learner is None
    assert Learner.objects.count() == 0


@pytest.mark.django_db
def test_resolve_existing_guest_learner_returns_the_existing_row():
    existing = resolve_or_create_guest_learner(public_id=None)
    before_count = Learner.objects.count()

    learner = resolve_existing_guest_learner(public_id=existing.public_id)

    assert learner is not None
    assert learner.pk == existing.pk
    assert Learner.objects.count() == before_count


@pytest.mark.django_db
def test_resolve_existing_guest_learner_never_returns_an_account_backed_learner():
    account = Account.objects.create_user(username="bob")
    account_learner = ensure_learner(account)

    learner = resolve_existing_guest_learner(public_id=account_learner.public_id)

    assert learner is None


@pytest.mark.django_db
def test_resolve_existing_learner_for_account_returns_none_when_none_exists():
    account = Account.objects.create_user(username="carol")

    learner = resolve_existing_learner_for_account(account)

    assert learner is None
    assert Learner.objects.count() == 0


@pytest.mark.django_db
def test_resolve_existing_learner_for_account_returns_the_existing_row():
    account = Account.objects.create_user(username="dave")
    existing = ensure_learner(account)
    before_count = Learner.objects.count()

    learner = resolve_existing_learner_for_account(account)

    assert learner is not None
    assert learner.pk == existing.pk
    assert Learner.objects.count() == before_count
