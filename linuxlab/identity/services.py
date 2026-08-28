from linuxlab.identity.models import Learner


def ensure_learner(account):
    learner, _ = Learner.objects.get_or_create(account=account)
    return learner
