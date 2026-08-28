from django.contrib.auth.models import AbstractUser


class Account(AbstractUser):
    # Bootstrap stub for AUTH_USER_MODEL only; full identity design (UUID
    # public_id, Learner, AnonymousLearner, contextual roles) is Task 03.
    pass
