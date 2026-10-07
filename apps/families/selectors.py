from apps.accounts.models import User

from .models import FamilyMember


def get_membership(*, user: User) -> FamilyMember | None:
    return FamilyMember.objects.select_related("family").filter(user=user).first()
