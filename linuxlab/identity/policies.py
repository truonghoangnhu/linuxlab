from django.db.models import Q

from linuxlab.identity.models import RoleAssignment

# Role -> actions it grants. Only actions actually required so far are
# wired up; the remaining approved roles (ADR-012, spec 11.3) exist as
# RoleAssignment.Role choices without speculative action sets.
ROLE_ACTIONS = {
    RoleAssignment.Role.INSTRUCTOR: {"learning.review"},
}


def can(actor, action, resource=None, context=None):
    allowed_roles = {role for role, actions in ROLE_ACTIONS.items() if action in actions}
    if not allowed_roles:
        return False

    assignments = RoleAssignment.objects.filter(account=actor, role__in=allowed_roles)
    if context is not None:
        assignments = assignments.filter(Q(context=context) | Q(context__isnull=True))
    else:
        assignments = assignments.filter(context__isnull=True)

    return assignments.exists()
