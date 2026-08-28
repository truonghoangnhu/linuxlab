import uuid

import pytest
from django.contrib.auth import get_user_model

from linuxlab.identity.models import AnonymousLearner, LearningContext, RoleAssignment
from linuxlab.identity.policies import can
from linuxlab.identity.services import ensure_learner

Account = get_user_model()


# --- A. Account stable identity ---------------------------------------------


@pytest.mark.django_db
def test_account_has_uuid_public_id():
    account = Account.objects.create(username="alice")
    assert isinstance(account.public_id, uuid.UUID)


@pytest.mark.django_db
def test_two_accounts_get_distinct_public_ids():
    first = Account.objects.create(username="alice")
    second = Account.objects.create(username="bob")
    assert first.public_id != second.public_id


@pytest.mark.django_db
def test_account_retains_local_integer_primary_key():
    account = Account.objects.create(username="alice")
    assert isinstance(account.pk, int)
    assert account.pk != account.public_id


# --- B. Account vs Learner identity ------------------------------------------


@pytest.mark.django_db
def test_account_and_learner_have_distinct_public_ids():
    account = Account.objects.create(username="alice")
    learner = ensure_learner(account)
    assert account.public_id != learner.public_id


# --- C. Learner without Account ----------------------------------------------


@pytest.mark.django_db
def test_learner_can_exist_without_account():
    from linuxlab.identity.models import Learner

    learner = Learner.objects.create(account=None)
    assert learner.account is None
    assert isinstance(learner.public_id, uuid.UUID)


# --- D. AnonymousLearner ------------------------------------------------------


@pytest.mark.django_db
def test_anonymous_learner_has_no_account_dependency():
    guest = AnonymousLearner.objects.create()
    assert isinstance(guest.public_id, uuid.UUID)
    assert not hasattr(guest, "account")


# --- E. LearningContext -------------------------------------------------------


@pytest.mark.django_db
def test_learning_contexts_have_distinct_stable_identity():
    context_a = LearningContext.objects.create(kind="personal", name="A")
    context_b = LearningContext.objects.create(kind="personal", name="B")
    assert isinstance(context_a.public_id, uuid.UUID)
    assert isinstance(context_b.public_id, uuid.UUID)
    assert context_a.public_id != context_b.public_id


# --- F. ensure_learner() -------------------------------------------------------


@pytest.mark.django_db
def test_ensure_learner_creates_and_is_idempotent():
    from linuxlab.identity.models import Learner

    account = Account.objects.create(username="alice")

    first = ensure_learner(account)
    second = ensure_learner(account)

    assert first.pk == second.pk
    assert Learner.objects.filter(account=account).count() == 1


# --- G. contextual RoleAssignment ---------------------------------------------


@pytest.mark.django_db
def test_contextual_instructor_role_is_scoped_to_its_context():
    account = Account.objects.create(username="alice")
    context_a = LearningContext.objects.create(kind="personal", name="A")
    context_b = LearningContext.objects.create(kind="personal", name="B")
    RoleAssignment.objects.create(
        account=account, role=RoleAssignment.Role.INSTRUCTOR, context=context_a
    )

    assert can(account, "learning.review", context=context_a) is True
    assert can(account, "learning.review", context=context_b) is False
    assert can(account, "learning.review", context=None) is False


# --- H. global assignment -----------------------------------------------------


@pytest.mark.django_db
def test_global_instructor_role_grants_with_and_without_context():
    account = Account.objects.create(username="alice")
    context_a = LearningContext.objects.create(kind="personal", name="A")
    RoleAssignment.objects.create(
        account=account, role=RoleAssignment.Role.INSTRUCTOR, context=None
    )

    assert can(account, "learning.review", context=context_a) is True
    assert can(account, "learning.review", context=None) is True


# --- I. unknown permission -----------------------------------------------------


@pytest.mark.django_db
def test_unknown_action_is_denied():
    account = Account.objects.create(username="alice")
    RoleAssignment.objects.create(
        account=account, role=RoleAssignment.Role.INSTRUCTOR, context=None
    )

    assert can(account, "exam.grade", context=None) is False


# --- J. no Django flag bypass ---------------------------------------------------


@pytest.mark.django_db
def test_is_staff_and_is_superuser_do_not_bypass_authorization():
    account = Account.objects.create(username="alice", is_staff=True, is_superuser=True)

    assert can(account, "learning.review", context=None) is False
