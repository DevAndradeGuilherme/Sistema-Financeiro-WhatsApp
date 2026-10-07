import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.accounts.models import User
from apps.accounts.selectors import get_active_user_by_phone
from apps.accounts.services import create_user

pytestmark = pytest.mark.django_db


def test_create_user_normalizes_phone():
    user = create_user(username="ana", phone_number="(31) 99999-8888")
    assert user.phone_number == "+5531999998888"
    assert not user.has_usable_password()


def test_create_user_without_phone_stores_null():
    user = create_user(username="staff", phone_number="")
    assert user.phone_number is None
    create_user(username="staff2")  # multiple users without phone are allowed


def test_phone_is_unique_after_normalization():
    create_user(username="ana", phone_number="+5531999998888")
    with pytest.raises(ValidationError) as exc:
        create_user(username="ana2", phone_number="553199998888")
    assert "phone_number" in exc.value.message_dict


def test_phone_unique_constraint_in_database():
    User.objects.create(username="a", phone_number="+5531999998888")
    with pytest.raises(IntegrityError):
        User.objects.create(username="b", phone_number="+5531999998888")


def test_database_rejects_non_canonical_phone():
    with pytest.raises(IntegrityError):
        User.objects.create(username="raw", phone_number="31 99999-8888")


def test_invalid_phone_rejected():
    with pytest.raises(ValidationError):
        create_user(username="x", phone_number="123")


def test_get_active_user_by_phone():
    user = create_user(username="ana", phone_number="+5531999998888")
    assert get_active_user_by_phone("553199998888") == user
    assert get_active_user_by_phone("lixo") is None
    user.is_active = False
    user.save()
    assert get_active_user_by_phone("+5531999998888") is None
