from django.core.exceptions import PermissionDenied, ValidationError
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
    # All supported membership mutations share this lock, including concurrent transfers.
    Family.objects.select_for_update(no_key=True).get(pk=family.pk)
    User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if FamilyMember.objects.filter(user=user).exists():
        raise ValidationError({"user": "Usuário já pertence a uma família."})
    if (
        role == FamilyMember.Role.MEMBER
        and not FamilyMember.objects.filter(family=family, role=FamilyMember.Role.ADMIN).exists()
    ):
        raise ValidationError(
            {"family": "A família precisa ter um administrador antes de receber membros."}
        )
    membership = FamilyMember(family=family, user=user, role=role)
    membership.full_clean()  # also validates the single-ADMIN constraint
    membership.save()
    return membership


def require_family_operator(*, actor: User, membership_permission: str) -> None:
    if not (
        actor.is_active
        and actor.is_staff
        and actor.has_perms(("families.change_family", membership_permission))
    ):
        raise PermissionDenied


def _single_admin(family: Family) -> FamilyMember:
    admins = list(FamilyMember.objects.filter(family=family, role=FamilyMember.Role.ADMIN))
    if len(admins) != 1:
        raise ValidationError("A família deve ter exatamente um administrador.")
    return admins[0]


@transaction.atomic
def transfer_family_admin(*, actor: User, family: Family, new_admin_user: User) -> int:
    """Serialize transfers and membership removals; return the previous administrator's id."""
    require_family_operator(actor=actor, membership_permission="families.change_familymember")
    family = Family.objects.select_for_update(no_key=True).get(pk=family.pk)
    current = _single_admin(family)
    target = FamilyMember.objects.filter(family=family, user=new_admin_user).first()
    if target is None or target.role != FamilyMember.Role.MEMBER:
        raise ValidationError("Escolha um MEMBER atual desta família para receber a administração.")
    current.role = FamilyMember.Role.MEMBER
    current.save(update_fields=["role"])
    target.role = FamilyMember.Role.ADMIN
    target.save(update_fields=["role"])
    _single_admin(family)
    return current.user_id


@transaction.atomic
def remove_family_member(*, actor: User, family: Family, user: User) -> None:
    require_family_operator(actor=actor, membership_permission="families.delete_familymember")
    family = Family.objects.select_for_update(no_key=True).get(pk=family.pk)
    _single_admin(family)
    member = FamilyMember.objects.filter(family=family, user=user).first()
    if member is None:
        raise ValidationError("Usuário não pertence a esta família.")
    if member.is_admin:
        raise ValidationError("Transfira a administração antes de remover este usuário.")
    member.delete()
