"""Visibility rules between users. Never fed by AI output; always by the authenticated actor.

Family ADMIN only gains *read* access. Modification rules live in each domain
(e.g. ``apps.transactions.policies``) and are owner-only.
"""

from apps.accounts.models import User

from .selectors import get_membership


def can_view_user_data(*, actor: User, target_user: User) -> bool:
    if actor.pk is None or target_user.pk is None:
        return False
    if actor.pk == target_user.pk:
        return True
    actor_membership = get_membership(user=actor)
    if actor_membership is None or not actor_membership.is_admin:
        return False
    target_membership = get_membership(user=target_user)
    return (
        target_membership is not None and target_membership.family_id == actor_membership.family_id
    )
