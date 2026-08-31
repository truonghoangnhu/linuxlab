from linuxlab.identity.models import Learner


def ensure_learner(account):
    if account is None:
        raise ValueError("account is required")

    learner, _ = Learner.objects.get_or_create(account=account)
    return learner


def resolve_or_create_guest_learner(*, public_id=None):
    """Resolve an existing guest Learner (account=None) by its stable
    public_id, or mint a fresh one (T15-D27 corrective: identity-boundary
    finding). Guest identity has no Account dependency (ADR-006/ADR-012);
    this is the identity-owned counterpart to ensure_learner() for the
    anonymous/session-bound case.

    `public_id` must come from a caller's own trusted state (e.g. a
    server-side session value it previously set itself) -- never from an
    arbitrary request payload. A public_id that does not resolve to a
    genuine guest row (unknown, or an existing Account-backed Learner)
    never returns that other row; it mints a fresh guest instead, so a
    forged/mismatched id can never attach to someone else's identity.
    """
    if public_id is not None:
        learner = Learner.objects.filter(public_id=public_id, account__isnull=True).first()
        if learner is not None:
            return learner

    return Learner.objects.create(account=None)


def resolve_existing_guest_learner(*, public_id=None):
    """Fully read-only counterpart to resolve_or_create_guest_learner()
    (T15-D27 final corrective, read-only-authorization finding): resolves
    an EXISTING guest Learner (account=None) by its stable public_id, or
    None -- never creates a Learner. For authorizing access to an
    already-existing resource (e.g. an existing Attempt), where a denied
    request must leave zero trace: no new Learner row and no new session
    state.

    Never returns an Account-backed Learner even if `public_id` happens
    to match one -- an arbitrary/forged public_id can never authorize as
    someone else's registered identity.
    """
    if public_id is None:
        return None
    return Learner.objects.filter(public_id=public_id, account__isnull=True).first()


def resolve_existing_learner_for_account(account):
    """Fully read-only counterpart to ensure_learner() (T15-D27 final
    corrective, read-only-authorization finding): resolves the EXISTING
    Learner linked to `account`, or None if none exists yet -- never
    creates one. For authorizing access to an already-existing resource
    without side-effecting a first-time Learner creation."""
    if account is None:
        return None
    return Learner.objects.filter(account=account).first()
