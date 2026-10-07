import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.families.models import FamilyMember
from apps.families.policies import can_view_user_data
from apps.families.selectors import get_membership
from apps.families.services import add_family_member, create_family

pytestmark = pytest.mark.django_db


def test_create_family_makes_creator_admin(alice):
    family = create_family(name="Silva", admin=alice)
    membership = get_membership(user=alice)
    assert membership.family == family
    assert membership.role == FamilyMember.Role.ADMIN


def test_add_member(alice, bob):
    family = create_family(name="Silva", admin=alice)
    membership = add_family_member(family=family, user=bob)
    assert membership.role == FamilyMember.Role.MEMBER
    assert family.memberships.count() == 2


def test_user_belongs_to_at_most_one_family(alice, bob):
    create_family(name="Silva", admin=alice)
    other = create_family(name="Souza", admin=bob)
    with pytest.raises(ValidationError):
        add_family_member(family=other, user=alice)


def test_family_has_single_admin(alice, bob):
    family = create_family(name="Silva", admin=alice)
    with pytest.raises(ValidationError):
        add_family_member(family=family, user=bob, role=FamilyMember.Role.ADMIN)


def test_single_admin_enforced_by_database(alice, bob):
    family = create_family(name="Silva", admin=alice)
    with pytest.raises(IntegrityError):
        FamilyMember.objects.create(family=family, user=bob, role=FamilyMember.Role.ADMIN)


def test_visibility_policy(make_user):
    admin, member, other_member, outsider = (make_user() for _ in range(4))
    family = create_family(name="Silva", admin=admin)
    add_family_member(family=family, user=member)
    add_family_member(family=family, user=other_member)

    assert can_view_user_data(actor=member, target_user=member)
    assert can_view_user_data(actor=admin, target_user=member)
    assert not can_view_user_data(actor=member, target_user=other_member)
    assert not can_view_user_data(actor=member, target_user=admin)
    assert not can_view_user_data(actor=admin, target_user=outsider)
    assert not can_view_user_data(actor=outsider, target_user=member)


def test_admin_inline_requires_exactly_one_admin(admin_client, alice, bob):
    data = {
        "name": "Silva",
        "memberships-TOTAL_FORMS": "2",
        "memberships-INITIAL_FORMS": "0",
        "memberships-0-user": alice.pk,
        "memberships-0-role": "ADMIN",
        "memberships-1-user": bob.pk,
        "memberships-1-role": "ADMIN",
    }
    response = admin_client.post("/admin/families/family/add/", data)
    assert response.status_code == 200  # form redisplayed with error
    assert not FamilyMember.objects.exists()

    data["memberships-1-role"] = "MEMBER"
    response = admin_client.post("/admin/families/family/add/", data)
    assert response.status_code == 302
    assert FamilyMember.objects.count() == 2
