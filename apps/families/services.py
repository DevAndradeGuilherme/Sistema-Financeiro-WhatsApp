from django.core.exceptions import ValidationError
from django.db import transaction

from apps.accounts.models import User

from .models import Family, FamilyMember


@transaction.atomic
def create_family(*, name: str, admin: User) -> Family:
    """Create a family and its single ADMIN membership atomically."""
    family = Family(name=name.strip())
    family.full_clean()
    family.save()
    add_family_member(family=family, user=admin, role=FamilyMember.Role.ADMIN)
    return family


@transaction.atomic
def add_family_member(
    *, family: Family, user: User, role: str = FamilyMember.Role.MEMBER
) -> FamilyMember:
    if FamilyMember.objects.filter(user=user).exists():
        raise ValidationError({"user": "Usuário já pertence a uma família."})
    membership = FamilyMember(family=family, user=user, role=role)
    membership.full_clean()  # also validates the single-ADMIN constraint
    membership.save()
    return membership
