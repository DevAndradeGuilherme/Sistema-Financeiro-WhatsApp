from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError

from apps.categories.models import Category, CategoryKind, Subcategory
from apps.families.services import add_family_member, create_family
from apps.transactions.models import Transaction, TransactionSource, TransactionType
from apps.transactions.policies import can_modify_transaction
from apps.transactions.services import create_transaction, soft_delete_transaction
from common.dates import today_local

pytestmark = pytest.mark.django_db


def test_create_expense(alice, transport, fuel):
    txn = create_transaction(
        user=alice,
        type=TransactionType.EXPENSE,
        amount="10",
        category=transport,
        subcategory=fuel,
        description="  gasolina   posto ",
        transaction_date=date(2026, 10, 1),
        source=TransactionSource.TEXT,
    )
    txn.refresh_from_db()
    assert txn.type == TransactionType.EXPENSE
    assert txn.amount == Decimal("10.00")
    assert txn.subcategory == fuel
    assert txn.description == "gasolina posto"
    assert txn.family is None
    assert txn.deleted_at is None
    assert txn.created_at.tzinfo is not None


def test_create_income(alice, salary):
    txn = create_transaction(
        user=alice,
        type=TransactionType.INCOME,
        amount=Decimal("5000.00"),
        category=salary,
        source=TransactionSource.MANUAL,
    )
    assert txn.type == TransactionType.INCOME
    assert txn.transaction_date == today_local()


def test_both_kind_category_accepts_income_and_expense(alice, make_transaction):
    pix = Category.objects.create(name="Pix", slug="pix", kind=CategoryKind.BOTH)
    make_transaction(user=alice, category=pix, type=TransactionType.EXPENSE)
    make_transaction(user=alice, category=pix, type=TransactionType.INCOME)
    assert Transaction.objects.filter(category=pix).count() == 2


def test_decimal_preserves_cents(alice, food, make_transaction):
    txn = make_transaction(user=alice, category=food, amount="0.10")
    make_transaction(user=alice, category=food, amount="0.20")
    txn.refresh_from_db()
    assert txn.amount == Decimal("0.10")
    total = sum(t.amount for t in Transaction.objects.all())
    assert total == Decimal("0.30")


@pytest.mark.parametrize("amount", ["0", "0.00", Decimal("0")])
def test_zero_amount_rejected(alice, food, make_transaction, amount):
    with pytest.raises(ValidationError) as exc:
        make_transaction(user=alice, category=food, amount=amount)
    assert "amount" in exc.value.message_dict


@pytest.mark.parametrize("amount", ["-10", Decimal("-0.01"), -5])
def test_negative_amount_rejected(alice, food, make_transaction, amount):
    with pytest.raises(ValidationError) as exc:
        make_transaction(user=alice, category=food, amount=amount)
    assert "amount" in exc.value.message_dict


@pytest.mark.parametrize("amount", [10.5, "10.555", "abc", "NaN", "1000000.01"])
def test_invalid_amount_rejected(alice, food, make_transaction, amount):
    with pytest.raises(ValidationError) as exc:
        make_transaction(user=alice, category=food, amount=amount)
    assert "amount" in exc.value.message_dict


def test_database_rejects_non_positive_amount(alice, food):
    with pytest.raises(IntegrityError):
        Transaction.objects.create(
            user=alice,
            type=TransactionType.EXPENSE,
            amount=Decimal("0"),
            category=food,
            transaction_date=date(2026, 10, 1),
            source=TransactionSource.MANUAL,
        )


def test_category_incompatible_with_type_rejected(alice, salary, transport, make_transaction):
    with pytest.raises(ValidationError) as exc:
        make_transaction(user=alice, category=salary, type=TransactionType.EXPENSE)
    assert "category" in exc.value.message_dict
    with pytest.raises(ValidationError):
        make_transaction(user=alice, category=transport, type=TransactionType.INCOME)


def test_subcategory_of_correct_category_accepted(alice, transport, fuel, make_transaction):
    txn = make_transaction(user=alice, category=transport, subcategory=fuel)
    assert txn.subcategory_id == fuel.pk


def test_subcategory_of_other_category_rejected(alice, transport, food, make_transaction):
    restaurant = Subcategory.objects.create(category=food, name="Restaurante", slug="restaurante")
    with pytest.raises(ValidationError) as exc:
        make_transaction(user=alice, category=transport, subcategory=restaurant)
    assert "subcategory" in exc.value.message_dict


def test_model_clean_rejects_inconsistent_subcategory(alice, transport, food):
    restaurant = Subcategory.objects.create(category=food, name="Restaurante", slug="restaurante")
    txn = Transaction(
        user=alice,
        type=TransactionType.EXPENSE,
        amount=Decimal("1.00"),
        category=transport,
        subcategory=restaurant,
        transaction_date=date(2026, 10, 1),
        source=TransactionSource.ADMIN,
    )
    with pytest.raises(ValidationError):
        txn.full_clean()


def test_inactive_category_or_subcategory_rejected(alice, transport, fuel, make_transaction):
    fuel.is_active = False
    fuel.save()
    with pytest.raises(ValidationError):
        make_transaction(user=alice, category=transport, subcategory=fuel)
    transport.is_active = False
    transport.save()
    with pytest.raises(ValidationError):
        make_transaction(user=alice, category=transport)


def test_future_date_rejected(alice, food, make_transaction):
    with pytest.raises(ValidationError) as exc:
        make_transaction(
            user=alice, category=food, transaction_date=today_local() + timedelta(days=1)
        )
    assert "transaction_date" in exc.value.message_dict


def test_datetime_instead_of_date_rejected(alice, food, make_transaction):
    with pytest.raises(ValidationError):
        make_transaction(user=alice, category=food, transaction_date=datetime(2026, 10, 1, 12, 0))


def test_invalid_type_and_source_rejected(alice, food, make_transaction):
    with pytest.raises(ValidationError) as exc:
        make_transaction(user=alice, category=food, type="TRANSFER", source="EMAIL")
    assert {"type", "source"} <= set(exc.value.message_dict)


def test_inactive_user_rejected(alice, food, make_transaction):
    alice.is_active = False
    alice.save()
    with pytest.raises(ValidationError):
        make_transaction(user=alice, category=food)


def test_family_derived_from_membership(alice, bob, food, make_transaction):
    family = create_family(name="Silva", admin=alice)
    txn = make_transaction(user=alice, category=food)
    assert txn.family == family

    other_family = create_family(name="Souza", admin=bob)
    with pytest.raises(ValidationError) as exc:
        make_transaction(user=alice, category=food, family=other_family)
    assert "family" in exc.value.message_dict


def test_idempotency_key_prevents_duplicates(alice, bob, food, make_transaction):
    first = make_transaction(user=alice, category=food, idempotency_key="whatsapp:inst:ABC")
    second = make_transaction(user=alice, category=food, idempotency_key="whatsapp:inst:ABC")
    assert first.pk == second.pk
    assert Transaction.objects.count() == 1
    with pytest.raises(ValidationError):
        make_transaction(user=bob, category=food, idempotency_key="whatsapp:inst:ABC")


def test_soft_delete_by_owner(alice, food, make_transaction):
    txn = make_transaction(user=alice, category=food)
    soft_delete_transaction(actor=alice, transaction_id=txn.pk)
    txn.refresh_from_db()
    assert txn.deleted_at is not None
    assert Transaction.objects.filter(pk=txn.pk).exists()  # not physically deleted
    assert not Transaction.objects.active().filter(pk=txn.pk).exists()
    with pytest.raises(PermissionDenied):
        soft_delete_transaction(actor=alice, transaction_id=txn.pk)  # already deleted


def test_other_user_cannot_soft_delete(alice, bob, food, make_transaction):
    txn = make_transaction(user=alice, category=food)
    with pytest.raises(PermissionDenied):
        soft_delete_transaction(actor=bob, transaction_id=txn.pk)
    txn.refresh_from_db()
    assert txn.deleted_at is None


def test_family_admin_cannot_modify_member_transaction(alice, bob, food, make_transaction):
    family = create_family(name="Silva", admin=alice)
    add_family_member(family=family, user=bob)
    member_txn = make_transaction(user=bob, category=food)

    assert not can_modify_transaction(actor=alice, transaction=member_txn)
    assert can_modify_transaction(actor=bob, transaction=member_txn)
    with pytest.raises(PermissionDenied):
        soft_delete_transaction(actor=alice, transaction_id=member_txn.pk)
