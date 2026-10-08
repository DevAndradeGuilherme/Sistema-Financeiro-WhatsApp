from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.test import Client, RequestFactory

from apps.accounts.models import User
from apps.families.models import Family, FamilyMember
from apps.families.services import (
    add_family_member,
    create_family,
    remove_family_member,
    transfer_family_admin,
)

pytestmark = pytest.mark.django_db
ADMIN, MEMBER = FamilyMember.Role.ADMIN, FamilyMember.Role.MEMBER


@pytest.fixture
def family(alice, bob):
    family = create_family(name="Família operacional DEV", admin=alice)
    add_family_member(family=family, user=bob)
    return family


def operation_url(family, operation):
    return f"/admin/families/family/{family.pk}/{operation}/"


def assert_admin(family, user):
    assert family.memberships.filter(role=ADMIN).get().user_id == user.pk


def test_creation_logs_initial_admin_and_does_not_grant_permissions(admin_client, alice):
    response = admin_client.post(
        "/admin/families/family/add/",
        {
            "name": "Família nova DEV",
            "admin_user": alice.pk,
        },
    )
    assert response.status_code == 302
    family = Family.objects.get(name="Família nova DEV")
    assert_admin(family, alice)
    entry = LogEntry.objects.get(object_id=str(family.pk), action_flag=ADDITION)
    assert f"user_id={alice.pk}" in entry.change_message
    alice.refresh_from_db()
    assert not alice.is_staff and not alice.is_superuser
    assert not alice.user_permissions.exists() and not alice.groups.exists()


def test_creation_with_existing_member_is_rejected(admin_client, family, bob):
    response = admin_client.post(
        "/admin/families/family/add/",
        {
            "name": "Inválida DEV",
            "admin_user": bob.pk,
        },
    )
    assert response.status_code == 200
    assert not Family.objects.filter(name="Inválida DEV").exists()


def test_add_member_forces_member_role_and_preserves_django_permissions(
    admin_client, family, make_user
):
    user = make_user()
    response = admin_client.post(
        operation_url(family, "add-member"),
        {
            "user": user.pk,
            "role": ADMIN,
            "is_staff": "on",
            "is_superuser": "on",
        },
    )
    assert response.status_code == 302
    assert family.memberships.get(user=user).role == MEMBER
    user.refresh_from_db()
    assert not user.is_staff and not user.is_superuser
    assert not user.user_permissions.exists() and not user.groups.exists()


@pytest.mark.parametrize("other_family", [False, True])
def test_add_duplicate_or_other_family_member_rejected(
    admin_client, family, bob, make_user, other_family
):
    user = bob
    if other_family:
        user = make_user()
        create_family(name="Outra DEV", admin=user)
    original = user.family_membership.pk
    response = admin_client.post(operation_url(family, "add-member"), {"user": user.pk})
    assert response.status_code == 200
    assert FamilyMember.objects.get(user=user).pk == original


def test_remove_member_preserves_historical_snapshot(
    admin_client, family, bob, transport, make_transaction
):
    txn = make_transaction(user=bob, category=transport)
    response = admin_client.post(operation_url(family, "remove-member"), {"user": bob.pk})
    assert response.status_code == 302
    assert not FamilyMember.objects.filter(user=bob).exists()
    txn.refresh_from_db()
    assert txn.family_id == family.pk and txn.user_id == bob.pk
    assert txn.changes.count() == 1


def test_remove_admin_explains_transfer_requirement(admin_client, family, alice):
    response = admin_client.post(operation_url(family, "remove-member"), {"user": alice.pk})
    assert response.status_code == 200
    assert "Transfira a administração antes de remover este usuário." in response.content.decode()
    assert_admin(family, alice)


def test_transfer_records_ids_and_preserves_permissions(
    admin_client, admin_user, family, alice, bob
):
    response = admin_client.post(operation_url(family, "transfer-admin"), {"user": bob.pk})
    assert response.status_code == 302
    assert_admin(family, bob)
    assert family.memberships.get(user=alice).role == MEMBER
    log = LogEntry.objects.get(action_flag=CHANGE, object_id=str(family.pk))
    assert log.user_id == admin_user.pk
    assert f"user_id={alice.pk}" in log.change_message
    assert f"user_id={bob.pk}" in log.change_message
    assert alice.phone_number not in log.change_message
    assert bob.phone_number not in log.change_message
    for user in (alice, bob):
        user.refresh_from_db()
        assert not user.is_staff and not user.is_superuser
        assert not user.user_permissions.exists() and not user.groups.exists()


@pytest.mark.parametrize("target", ["outsider", "other_family", "current_admin", "missing"])
def test_transfer_rejects_invalid_destination(
    admin_client, admin_user, family, alice, make_user, target
):
    user = alice if target == "current_admin" else make_user()
    if target == "other_family":
        create_family(name="Outra DEV", admin=user)
    target_id = user.pk if target != "missing" else 999999
    response = admin_client.post(operation_url(family, "transfer-admin"), {"user": target_id})
    assert response.status_code == 200
    with pytest.raises(ValidationError):
        transfer_family_admin(actor=admin_user, family=family, new_admin_user=user)
    assert_admin(family, alice)
    assert not LogEntry.objects.filter(action_flag=CHANGE).exists()


def test_transfer_refuses_missing_initial_admin(admin_user, bob):
    family = Family.objects.create(name="Inválida DEV")
    FamilyMember.objects.create(family=family, user=bob)
    with pytest.raises(ValidationError, match="exatamente um"):
        transfer_family_admin(actor=admin_user, family=family, new_admin_user=bob)
    assert family.memberships.get().role == MEMBER


def test_transfer_rolls_back_mid_operation(admin_client, family, alice, bob):
    save = FamilyMember.save

    def fail_promotion(member, *args, **kwargs):
        if member.user_id == bob.pk and member.role == ADMIN:
            assert connection.in_atomic_block
            assert not family.memberships.filter(role=ADMIN).exists()
            raise ValidationError("Falha simulada na promoção.")
        return save(member, *args, **kwargs)

    with patch.object(FamilyMember, "save", fail_promotion):
        response = admin_client.post(operation_url(family, "transfer-admin"), {"user": bob.pk})
    assert response.status_code == 200
    assert_admin(family, alice)
    assert family.memberships.get(user=bob).role == MEMBER
    assert not LogEntry.objects.filter(action_flag=CHANGE).exists()


def test_log_failure_rolls_back_transfer(admin_client, family, alice, bob):
    with patch.object(LogEntry.objects, "log_actions", side_effect=ValidationError("Falha no log")):
        response = admin_client.post(operation_url(family, "transfer-admin"), {"user": bob.pk})
    assert response.status_code == 200
    assert_admin(family, alice)


@pytest.mark.parametrize("operation", ["add-member", "remove-member", "transfer-admin"])
def test_get_is_safe_and_post_requires_csrf(admin_user, family, alice, bob, operation):
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin_user)
    url = operation_url(family, operation)
    assert client.get(url, {"user": bob.pk}).status_code == 200
    assert_admin(family, alice)
    assert client.post(url, {"user": bob.pk}).status_code == 403
    assert_admin(family, alice)
    assert family.memberships.count() == 2


@pytest.mark.parametrize("role", ["admin", "member"])
def test_family_role_does_not_allow_admin_login(client, family, alice, bob, role):
    user = alice if role == "admin" else bob
    client.force_login(user)
    assert client.get("/admin/").status_code == 302
    assert client.post(operation_url(family, "transfer-admin"), {"user": bob.pk}).status_code == 302
    with pytest.raises(PermissionDenied):
        transfer_family_admin(actor=user, family=family, new_admin_user=bob)
    with pytest.raises(PermissionDenied):
        remove_family_member(actor=user, family=family, user=bob)
    assert_admin(family, alice)


@pytest.mark.parametrize(
    "operation,permission",
    [
        ("add-member", "add_familymember"),
        ("remove-member", "delete_familymember"),
        ("transfer-admin", "change_familymember"),
    ],
)
def test_staff_requires_both_permissions(client, family, bob, operation, permission):
    operator = User.objects.create_user(username="operator", is_staff=True)
    operator.user_permissions.add(Permission.objects.get(codename="change_family"))
    client.force_login(operator)
    url = operation_url(family, operation)
    assert client.post(url, {"user": bob.pk}).status_code == 403
    operator.user_permissions.add(Permission.objects.get(codename=permission))
    assert client.get(url).status_code == 200
    operator.user_permissions.remove(Permission.objects.get(codename="change_family"))
    assert client.post(url, {"user": bob.pk}).status_code == 403


def test_authorized_staff_can_transfer(client, family, bob):
    operator = User.objects.create_user(username="operator", is_staff=True)
    operator.user_permissions.set(
        Permission.objects.filter(codename__in=["change_family", "change_familymember"])
    )
    client.force_login(operator)
    assert client.post(operation_url(family, "transfer-admin"), {"user": bob.pk}).status_code == 302
    assert_admin(family, bob)


def test_family_delete_and_bulk_delete_blocked(
    admin_client, family, alice, transport, make_transaction
):
    txn = make_transaction(user=alice, category=transport)
    assert (
        admin_client.post(
            f"/admin/families/family/{family.pk}/delete/", {"post": "yes"}
        ).status_code
        == 403
    )
    response = admin_client.get("/admin/families/family/")
    assert b"delete_selected" not in response.content
    assert_admin(family, alice)
    txn.refresh_from_db()
    assert txn.family_id == family.pk


def test_family_listing_has_constant_queries(admin_user, make_user, django_assert_num_queries):
    for index in range(4):
        family = create_family(name=f"Família DEV {index}", admin=make_user())
        add_family_member(family=family, user=make_user())
    model_admin = admin.site._registry[Family]
    request = RequestFactory().get("/admin/families/family/")
    request.user = admin_user
    with django_assert_num_queries(2):
        for family in model_admin.get_queryset(request):
            assert model_admin.current_admin(family)
            assert model_admin.member_count(family) == 2
            assert model_admin.members(family)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("same_target", [False, True])
def test_concurrent_transfers_keep_exactly_one_admin(
    admin_user, family, bob, make_user, same_target
):
    other = make_user()
    add_family_member(family=family, user=other)
    barrier = Barrier(2)

    def transfer(user):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                transfer_family_admin(actor=admin_user, family=family, new_admin_user=user)
            except ValidationError:
                return "rejected"
            return "ok"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(transfer, user) for user in (bob, bob if same_target else other)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == (["ok", "rejected"] if same_target else ["ok", "ok"])
    assert family.memberships.filter(role=ADMIN).count() == 1
    assert family.memberships.filter(role=MEMBER).count() == 2
