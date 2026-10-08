"""V1.2: Django Admin keeps family invariants (one ADMIN, one family per user)."""

import pytest

from apps.families.models import Family, FamilyMember
from apps.families.services import add_family_member, create_family

pytestmark = pytest.mark.django_db

ADMIN, MEMBER = FamilyMember.Role.ADMIN, FamilyMember.Role.MEMBER


@pytest.fixture
def family(alice, bob):
    fam = create_family(name="Silva", admin=alice)
    add_family_member(family=fam, user=bob)
    return fam


def inline_data(family, rows):
    data = {
        "name": family.name,
        "memberships-TOTAL_FORMS": str(len(rows)),
        "memberships-INITIAL_FORMS": str(len(rows)),
    }
    for index, (membership, role) in enumerate(rows):
        data.update(
            {
                f"memberships-{index}-id": membership.pk,
                f"memberships-{index}-family": family.pk,
                f"memberships-{index}-user": membership.user_id,
                f"memberships-{index}-role": role,
            }
        )
    return data


def test_swap_admin_in_one_operation(admin_client, family, alice, bob):
    # The explicit operation replaces arbitrary role edits in the old inline.
    response = admin_client.post(
        f"/admin/families/family/{family.pk}/transfer-admin/",
        {"user": bob.pk},
    )
    assert response.status_code == 302, response.content
    assert FamilyMember.objects.get(role=ADMIN, family=family).user == bob


def test_inline_rejects_two_admins_or_none(admin_client, family, alice, bob):
    bob_m = FamilyMember.objects.get(user=bob)
    alice_m = FamilyMember.objects.get(user=alice)
    for roles in ((ADMIN, ADMIN), (MEMBER, MEMBER)):
        response = admin_client.post(
            f"/admin/families/family/{family.pk}/change/",
            inline_data(family, [(alice_m, roles[0]), (bob_m, roles[1])]),
        )
        assert response.status_code == 302  # forged inline data cannot alter memberships
    assert FamilyMember.objects.get(role=ADMIN, family=family).user == alice


def test_admin_rejects_family_without_admin(admin_client):
    response = admin_client.post(
        "/admin/families/family/add/",
        {
            "name": "Sem administrador",
            "memberships-TOTAL_FORMS": "0",
            "memberships-INITIAL_FORMS": "0",
        },
    )
    assert response.status_code == 200
    assert not Family.objects.filter(name="Sem administrador").exists()


def test_standalone_cannot_add_member_to_family_without_admin(admin_client, family, make_user):
    orphan = Family.objects.create(name="Família sem admin")
    newcomer = make_user()
    response = admin_client.post(
        "/admin/families/familymember/add/",
        {"family": orphan.pk, "user": newcomer.pk, "role": MEMBER},
    )
    assert response.status_code == 403
    assert not FamilyMember.objects.filter(family=orphan).exists()


def test_standalone_cannot_demote_admin(admin_client, family, alice):
    membership = FamilyMember.objects.get(user=alice)
    response = admin_client.post(
        f"/admin/families/familymember/{membership.pk}/change/",
        {"family": family.pk, "user": alice.pk, "role": MEMBER},
    )
    assert response.status_code == 403
    membership.refresh_from_db()
    assert membership.role == ADMIN


def test_standalone_cannot_add_second_admin(admin_client, family, make_user):
    newcomer = make_user()
    response = admin_client.post(
        "/admin/families/familymember/add/",
        {"family": family.pk, "user": newcomer.pk, "role": ADMIN},
    )
    assert response.status_code == 403
    assert FamilyMember.objects.filter(family=family, role=ADMIN).count() == 1


def test_user_in_at_most_one_family(admin_client, family, alice, make_user):
    other_admin = make_user()
    other = create_family(name="Outra", admin=other_admin)
    response = admin_client.post(
        "/admin/families/familymember/add/",
        {"family": other.pk, "user": alice.pk, "role": MEMBER},
    )
    assert response.status_code == 403
    assert FamilyMember.objects.filter(user=alice).count() == 1


def test_cannot_delete_admin_while_members_remain(admin_client, family, alice, bob):
    admin_m = FamilyMember.objects.get(user=alice)
    url = f"/admin/families/familymember/{admin_m.pk}/delete/"
    assert admin_client.post(url, {"post": "yes"}).status_code == 403
    assert FamilyMember.objects.filter(pk=admin_m.pk).exists()
    # Bulk delete is not offered (it would bypass the check).
    changelist = admin_client.get("/admin/families/familymember/")
    assert b"delete_selected" not in changelist.content
    # A regular member can be removed.
    response = admin_client.post(
        f"/admin/families/family/{family.pk}/remove-member/", {"user": bob.pk}
    )
    assert response.status_code == 302


def test_cannot_delete_only_admin_standalone(admin_client, alice):
    family = create_family(name="Somente admin", admin=alice)
    admin_m = FamilyMember.objects.get(user=alice)
    response = admin_client.post(
        f"/admin/families/familymember/{admin_m.pk}/delete/", {"post": "yes"}
    )
    assert response.status_code == 403
    assert FamilyMember.objects.filter(family=family, role=ADMIN).count() == 1
