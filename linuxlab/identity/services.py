from linuxlab.identity.models import Learner


def ensure_learner(account):
    if account is None:
        raise ValueError("account is required")

    learner, _ = Learner.objects.get_or_create(account=account)
    return learner
