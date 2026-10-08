from unittest.mock import patch

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.test import RequestFactory

from apps.accounts.models import User
from apps.families.services import add_family_member, create_family
from apps.transactions.models import Transaction, TransactionChange
from apps.whatsapp.models import InboundMessage, MessageType
from apps.whatsapp.tests import payloads

pytestmark = pytest.mark.django_db


def user_data(user, active=True):
    data = {"username": user.username, "phone_number": user.phone_number or ""}
    if active:
        data["is_active"] = "on"
    return data


@pytest.mark.parametrize("raw", ["(31) 99999-0001", "00 55 (31) 9999-0001", "+55 (31) 99999-0001"])
def test_admin_normalizes_formatted_phones_before_length_validation(admin_client, raw):
    response = admin_client.post(
        "/admin/accounts/user/add/",
        {
            "username": "canal-dev",
            "phone_number": raw,
            "usable_password": "false",
        },
    )
    assert response.status_code == 302
    user = User.objects.get(username="canal-dev")
    assert user.phone_number == "+5531999990001"
    assert not user.has_usable_password()
    assert not user.is_staff and not user.is_superuser


@pytest.mark.parametrize("editing", [False, True])
def test_admin_duplicate_canonical_phone_rejected(admin_client, alice, bob, editing):
    data = {
        "username": "duplicate-dev",
        "phone_number": "00 55 (31) 9999-0001",
        "usable_password": "false",
    }
    url = f"/admin/accounts/user/{bob.pk}/change/" if editing else "/admin/accounts/user/add/"
    response = admin_client.post(url, data)
    assert response.status_code == 200
    assert "phone_number" in response.context["adminform"].form.errors
    assert User.objects.filter(phone_number=alice.phone_number).count() == 1
    bob.refresh_from_db()
    assert bob.phone_number == "+5531999990002"


def test_edit_normalizes_phone_and_never_exposes_hash(admin_client, bob):
    bob.set_password("only-for-tests-951")
    bob.save()
    data = user_data(bob)
    data["phone_number"] = "00 55 (31) 9999-0003"
    response = admin_client.post(f"/admin/accounts/user/{bob.pk}/change/", data)
    assert response.status_code == 302
    bob.refresh_from_db()
    assert bob.phone_number == "+5531999990003"
    response = admin_client.get(f"/admin/accounts/user/{bob.pk}/change/")
    assert bob.password not in response.content.decode()
    assert "only-for-tests-951" not in response.content.decode()
    assert b"Configurar senha" in response.content


def test_creation_defaults_to_no_password_but_supports_explicit_password(admin_client):
    response = admin_client.get("/admin/accounts/user/add/")
    assert response.context["adminform"].form.fields["usable_password"].initial == "false"
    response = admin_client.post(
        "/admin/accounts/user/add/",
        {
            "username": "login-dev",
            "usable_password": "true",
            "password1": "test-login-592-only",
            "password2": "test-login-592-only",
        },
    )
    assert response.status_code == 302
    assert User.objects.get(username="login-dev").check_password("test-login-592-only")


def test_deactivation_and_reactivation_preserve_all_history(
    admin_client, alice, bob, transport, make_transaction
):
    family = create_family(name="Família DEV", admin=alice)
    membership = add_family_member(family=family, user=bob)
    txn = make_transaction(user=bob, category=transport)
    inbound = InboundMessage.objects.create(
        user=bob,
        instance="test-instance",
        provider_message_id="history-dev",
        message_type=MessageType.TEXT,
        transaction=txn,
    )
    original_txn = Transaction.objects.values().get(pk=txn.pk)
    original_changes = list(TransactionChange.objects.filter(transaction=txn).values())
    original_inbound = InboundMessage.objects.values().get(pk=inbound.pk)
    for active in (False, True):
        response = admin_client.post(
            f"/admin/accounts/user/{bob.pk}/change/", user_data(bob, active)
        )
        assert response.status_code == 302
        bob.refresh_from_db()
        assert bob.is_active is active
        assert bob.family_membership.pk == membership.pk
        assert Transaction.objects.values().get(pk=txn.pk) == original_txn
        assert list(TransactionChange.objects.filter(transaction=txn).values()) == original_changes
        assert InboundMessage.objects.values().get(pk=inbound.pk) == original_inbound


@pytest.mark.parametrize("kind", ["text", "audio", "image"])
def test_admin_deactivated_user_blocked_before_pipeline(admin_client, client, alice, kind):
    assert (
        admin_client.post(
            f"/admin/accounts/user/{alice.pk}/change/", user_data(alice, False)
        ).status_code
        == 302
    )
    with (
        patch("apps.whatsapp.views.handle_ready_message") as text_pipeline,
        patch("apps.whatsapp.views.handle_audio_message") as audio_pipeline,
        patch("apps.whatsapp.views.handle_image_message") as image_pipeline,
    ):
        response = client.post(
            "/api/v1/webhooks/evolution/",
            getattr(payloads, kind)(),
            content_type="application/json",
            headers={"X-Webhook-Secret": "test-webhook-secret"},
        )
    assert response.status_code == 200
    assert InboundMessage.objects.get().ignore_reason == InboundMessage.IgnoreReason.INACTIVE_USER
    text_pipeline.assert_not_called()
    audio_pipeline.assert_not_called()
    image_pipeline.assert_not_called()
    assert not Transaction.objects.exists()


@pytest.mark.parametrize("history", [False, True])
def test_user_delete_blocked_even_for_superuser(
    admin_client, alice, transport, make_transaction, history
):
    family = create_family(name="Família DEV", admin=alice)
    if history:
        make_transaction(user=alice, category=transport)
    response = admin_client.post(f"/admin/accounts/user/{alice.pk}/delete/", {"post": "yes"})
    assert response.status_code == 403
    assert User.objects.filter(pk=alice.pk).exists()
    assert family.memberships.get().is_admin
    assert b"delete_selected" not in admin_client.get("/admin/accounts/user/").content


def test_staff_cannot_grant_permissions_or_take_over_staff(client, admin_user, bob):
    staff = User.objects.create_user(username="operator", is_staff=True)
    staff.user_permissions.set(Permission.objects.filter(content_type__app_label="accounts"))
    other = User.objects.create_user(username="other-staff", is_staff=True)
    client.force_login(staff)
    data = user_data(staff)
    data.update(
        {
            "is_superuser": "on",
            "user_permissions": list(Permission.objects.values_list("pk", flat=True)),
        }
    )
    original_perms = set(staff.user_permissions.values_list("pk", flat=True))
    assert client.post(f"/admin/accounts/user/{staff.pk}/change/", data).status_code == 302
    staff.refresh_from_db()
    assert not staff.is_superuser
    assert set(staff.user_permissions.values_list("pk", flat=True)) == original_perms
    for target in (other, admin_user):
        assert (
            client.post(f"/admin/accounts/user/{target.pk}/change/", user_data(target)).status_code
            == 403
        )
        assert client.post(f"/admin/accounts/user/{target.pk}/password/", {}).status_code == 403
    data = {
        "username": "injected-dev",
        "usable_password": "false",
        "is_staff": "on",
        "is_superuser": "on",
    }
    assert client.post("/admin/accounts/user/add/", data).status_code == 302
    created = User.objects.get(username="injected-dev")
    assert not created.is_staff and not created.is_superuser


def test_user_listing_family_filter_search_and_constant_queries(
    admin_client, admin_user, alice, bob, django_assert_num_queries
):
    family = create_family(name="Família DEV", admin=alice)
    add_family_member(family=family, user=bob)
    model_admin = admin.site._registry[User]
    request = RequestFactory().get("/admin/accounts/user/")
    request.user = admin_user
    with django_assert_num_queries(1):
        for user in model_admin.get_queryset(request):
            model_admin.current_family(user)
            model_admin.family_role(user)
    response = admin_client.get("/admin/accounts/user/", {"has_family": "yes"})
    assert set(response.context["cl"].queryset.values_list("pk", flat=True)) == {alice.pk, bob.pk}
    response = admin_client.get("/admin/accounts/user/", {"has_family": "no"})
    assert set(response.context["cl"].queryset.values_list("pk", flat=True)) == {admin_user.pk}
    response = admin_client.get("/admin/accounts/user/", {"q": alice.phone_number})
    assert list(response.context["cl"].queryset) == [alice]
