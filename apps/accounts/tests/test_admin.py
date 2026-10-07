import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(client):
    user = User.objects.create_user(username="staff", password="pw-123456", is_staff=True)
    user.user_permissions.set(Permission.objects.filter(content_type__app_label="accounts"))
    client.force_login(user)
    return user


def test_superuser_can_create_user_with_normalized_phone(admin_client):
    response = admin_client.post(
        "/admin/accounts/user/add/",
        {
            "username": "novo",
            "phone_number": "(31) 98888-7777",
            "usable_password": "false",
            "password1": "",
            "password2": "",
        },
    )
    assert response.status_code == 302, response.content
    assert User.objects.get(username="novo").phone_number == "+5531988887777"


def test_staff_cannot_escalate_own_privileges(client, staff):
    response = client.post(
        f"/admin/accounts/user/{staff.pk}/change/",
        {
            "username": "staff",
            "is_active": "on",
            "is_staff": "on",
            "is_superuser": "on",
            "date_joined_0": "2026-01-01",
            "date_joined_1": "00:00:00",
        },
    )
    assert response.status_code in (200, 302)
    staff.refresh_from_db()
    assert staff.is_superuser is False


def test_staff_cannot_change_superuser(client, staff, admin_user):
    response = client.get(f"/admin/accounts/user/{admin_user.pk}/change/")
    # Rendered read-only (view permission) — no change form submission allowed.
    response = client.post(
        f"/admin/accounts/user/{admin_user.pk}/change/", {"username": "hijacked"}
    )
    assert response.status_code == 403
    admin_user.refresh_from_db()
    assert admin_user.username == "admin"


def test_staff_cannot_delete_users(client, staff, make_user):
    target = make_user()
    response = client.post(f"/admin/accounts/user/{target.pk}/delete/", {"post": "yes"})
    assert response.status_code == 403
    assert User.objects.filter(pk=target.pk).exists()
