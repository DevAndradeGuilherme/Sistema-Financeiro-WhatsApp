"""V1.1 domain: update, audit trail, undo, comparison and reference search."""

import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connections

from apps.categories.models import Subcategory
from apps.families.services import add_family_member, create_family
from apps.transactions import selectors
from apps.transactions.models import TransactionChange
from apps.transactions.services import (
    soft_delete_transaction,
    undo_last_change,
    update_transaction,
)
from common.dates import today_local

pytestmark = pytest.mark.django_db
Action = TransactionChange.Action


@pytest.fixture
def txn(alice, transport, fuel, make_transaction):
    return make_transaction(
        user=alice, category=transport, subcategory=fuel, amount="10.00", description="gasolina"
    )


# --- Audit ---------------------------------------------------------------------------------


def test_create_records_audit(alice, txn):
    change = TransactionChange.objects.get()
    assert (change.action, change.user, change.transaction) == (Action.CREATE, alice, txn)
    assert change.before is None
    assert change.after["amount"] == "10.00"  # money as string


def test_soft_delete_records_audit(alice, txn):
    soft_delete_transaction(actor=alice, transaction_id=txn.pk)
    assert TransactionChange.objects.filter(action=Action.DELETE, transaction=txn).exists()


def test_undo_row_requires_target(alice, txn):
    with pytest.raises(IntegrityError):
        TransactionChange.objects.create(transaction=txn, user=alice, action=Action.UNDO)


def test_change_cannot_be_undone_twice_at_database_level(alice, txn):
    create = TransactionChange.objects.get()
    TransactionChange.objects.create(
        transaction=txn, user=alice, action=Action.UNDO, reverts=create
    )
    with pytest.raises(IntegrityError):
        TransactionChange.objects.create(
            transaction=txn, user=alice, action=Action.UNDO, reverts=create
        )


# --- UPDATE --------------------------------------------------------------------------------


def test_update_amount(alice, txn):
    result = update_transaction(user=alice, transaction_id=txn.pk, amount="15", origin="msg:1")
    txn.refresh_from_db()
    assert txn.amount == Decimal("15.00")
    assert result.before["amount"] == "10.00"
    change = result.change
    assert (change.action, change.before["amount"], change.after["amount"], change.origin) == (
        Action.UPDATE,
        "10.00",
        "15.00",
        "msg:1",
    )


def test_update_description(alice, txn):
    update_transaction(user=alice, transaction_id=txn.pk, description="  posto  shell ")
    txn.refresh_from_db()
    assert txn.description == "posto shell"


def test_update_category_drops_foreign_subcategory(alice, txn, food):
    update_transaction(user=alice, transaction_id=txn.pk, category=food)
    txn.refresh_from_db()
    assert txn.category == food
    assert txn.subcategory is None


def test_update_subcategory(alice, txn, transport):
    uber = Subcategory.objects.create(category=transport, name="Uber", slug="uber")
    update_transaction(user=alice, transaction_id=txn.pk, subcategory=uber)
    txn.refresh_from_db()
    assert txn.subcategory == uber


def test_update_date(alice, txn):
    yesterday = today_local() - timedelta(days=1)
    update_transaction(user=alice, transaction_id=txn.pk, transaction_date=yesterday)
    txn.refresh_from_db()
    assert txn.transaction_date == yesterday


def test_update_keeps_owner_family_type_source(alice, txn, food):
    family = txn.family
    update_transaction(user=alice, transaction_id=txn.pk, amount="99", category=food)
    txn.refresh_from_db()
    assert (txn.user, txn.family, txn.type, txn.source) == (alice, family, "EXPENSE", "MANUAL")


@pytest.mark.parametrize(
    "fields, field",
    [
        ({"amount": "0"}, "amount"),
        ({"amount": "-5"}, "amount"),
        ({"amount": 15.5}, "amount"),
        ({"amount": "1.234"}, "amount"),
        ({"transaction_date": date(2999, 1, 1)}, "transaction_date"),
    ],
)
def test_update_rejects_invalid_values(alice, txn, fields, field):
    with pytest.raises(ValidationError) as exc:
        update_transaction(user=alice, transaction_id=txn.pk, **fields)
    assert field in exc.value.message_dict
    txn.refresh_from_db()
    assert txn.amount == Decimal("10.00")
    assert TransactionChange.objects.filter(action=Action.UPDATE).count() == 0


def test_update_rejects_incompatible_category(alice, txn, salary):
    with pytest.raises(ValidationError) as exc:
        update_transaction(user=alice, transaction_id=txn.pk, category=salary)
    assert "category" in exc.value.message_dict


def test_update_rejects_subcategory_of_other_category(alice, txn, food):
    restaurant = Subcategory.objects.create(category=food, name="Restaurante", slug="restaurante")
    with pytest.raises(ValidationError) as exc:
        update_transaction(user=alice, transaction_id=txn.pk, subcategory=restaurant)
    assert "subcategory" in exc.value.message_dict


def test_update_rejects_other_users_transaction(bob, txn):
    with pytest.raises(PermissionDenied):
        update_transaction(user=bob, transaction_id=txn.pk, amount="1")
    txn.refresh_from_db()
    assert txn.amount == Decimal("10.00")


def test_family_admin_cannot_update_member_transaction(alice, bob, food, make_transaction):
    family = create_family(name="Silva", admin=alice)
    add_family_member(family=family, user=bob)
    member_txn = make_transaction(user=bob, category=food)
    with pytest.raises(PermissionDenied):
        update_transaction(user=alice, transaction_id=member_txn.pk, amount="1")


def test_update_rejects_soft_deleted(alice, txn):
    soft_delete_transaction(actor=alice, transaction_id=txn.pk)
    with pytest.raises(ValidationError):
        update_transaction(user=alice, transaction_id=txn.pk, amount="15")


def test_update_without_effective_change_records_nothing(alice, txn):
    result = update_transaction(user=alice, transaction_id=txn.pk, amount="10.00")
    assert result.change is None
    assert TransactionChange.objects.filter(action=Action.UPDATE).count() == 0


# --- UNDO ----------------------------------------------------------------------------------


def test_undo_update_then_create(alice, txn):
    update_transaction(user=alice, transaction_id=txn.pk, amount="15")

    first = undo_last_change(user=alice)
    txn.refresh_from_db()
    assert first.reverted.action == Action.UPDATE
    assert txn.amount == Decimal("10.00")
    assert txn.deleted_at is None

    second = undo_last_change(user=alice)
    txn.refresh_from_db()
    assert second.reverted.action == Action.CREATE
    assert txn.deleted_at is not None  # undo of creation = soft delete

    assert undo_last_change(user=alice) is None  # nothing left; UNDO itself is not undoable


def test_undo_restores_all_fields(alice, txn, food):
    yesterday = today_local() - timedelta(days=1)
    update_transaction(
        user=alice,
        transaction_id=txn.pk,
        amount="20",
        category=food,
        description="mercado",
        transaction_date=yesterday,
    )
    undo_last_change(user=alice)
    txn.refresh_from_db()
    assert (txn.amount, txn.category.slug, txn.subcategory.slug, txn.description) == (
        Decimal("10.00"),
        "transporte",
        "combustivel",
        "gasolina",
    )
    assert txn.transaction_date == date(2026, 10, 1)  # make_transaction default


def test_undo_is_a_stack_across_transactions(alice, transport, food, make_transaction):
    a = make_transaction(user=alice, category=transport, amount="1")
    b = make_transaction(user=alice, category=food, amount="2")
    assert undo_last_change(user=alice).transaction == b
    assert undo_last_change(user=alice).transaction == a


def test_undo_never_touches_other_users(alice, bob, txn):
    assert undo_last_change(user=bob) is None
    txn.refresh_from_db()
    assert txn.deleted_at is None


def test_undo_skips_deleted_transactions(alice, transport, make_transaction):
    a = make_transaction(user=alice, category=transport, amount="1")
    b = make_transaction(user=alice, category=transport, amount="2")
    soft_delete_transaction(actor=alice, transaction_id=b.pk)
    assert undo_last_change(user=alice).transaction == a


def test_undo_records_audit(alice, txn):
    result = undo_last_change(user=alice, origin="msg:9")
    undo = result.undo
    assert (undo.action, undo.reverts, undo.origin, undo.after) == (
        Action.UNDO,
        result.reverted,
        "msg:9",
        None,
    )
    assert undo.before["amount"] == "10.00"


@pytest.mark.django_db(transaction=True)
def test_concurrent_update_and_undo_stay_consistent(alice, transport, make_transaction):
    """'muda meu último gasto para 20' and 'desfazer' at the same time: serialized per user."""
    txn = make_transaction(user=alice, category=transport, amount="10")
    barrier = threading.Barrier(2)
    errors = []

    def run(fn):
        try:
            barrier.wait(timeout=5)
            fn()
        except (ValidationError, PermissionDenied):
            pass  # a legitimate rejection (e.g. undo deleted it first) is consistent
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [
        threading.Thread(
            target=run,
            args=(lambda: update_transaction(user=alice, transaction_id=txn.pk, amount="20"),),
        ),
        threading.Thread(target=run, args=(lambda: undo_last_change(user=alice),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    txn.refresh_from_db()
    changes = list(TransactionChange.objects.order_by("created_at", "id"))
    actions = [c.action for c in changes]
    if txn.deleted_at is not None:
        # undo ran first (undid CREATE); the update was rejected.
        assert actions == [Action.CREATE, Action.UNDO]
    else:
        # update ran first; undo reverted it.
        assert actions == [Action.CREATE, Action.UPDATE, Action.UNDO]
        assert txn.amount == Decimal("10.00")


# --- Selectors -------------------------------------------------------------------------


def test_compare_periods(alice, bob, transport, make_transaction):
    t = make_transaction
    t(user=alice, category=transport, amount="850.00", transaction_date=date(2026, 9, 10))
    t(user=alice, category=transport, amount="700.00", transaction_date=date(2026, 8, 10))
    t(user=bob, category=transport, amount="999.00", transaction_date=date(2026, 9, 10))
    result = selectors.compare_periods(
        user=alice,
        type="EXPENSE",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        compare_start_date=date(2026, 8, 1),
        compare_end_date=date(2026, 8, 31),
    )
    assert (result.current, result.previous, result.difference) == (
        Decimal("850.00"),
        Decimal("700.00"),
        Decimal("150.00"),
    )
    assert result.percent == Decimal("21.4")


@pytest.mark.parametrize(
    "current, previous, percent",
    [
        ("100.00", "0", None),
        ("0", "0", None),
        ("50.00", "50.00", Decimal("0.0")),
        ("50.00", "100.00", Decimal("-50.0")),
        ("1.00", "3.00", Decimal("-66.7")),
    ],
)
def test_comparison_percent(current, previous, percent):
    comparison = selectors.PeriodComparison(Decimal(current), Decimal(previous))
    assert comparison.percent == percent


def test_find_transactions_filters_and_limits(alice, bob, transport, fuel, food, make_transaction):
    today = today_local()
    t = make_transaction
    gas1 = t(
        user=alice,
        category=transport,
        subcategory=fuel,
        amount="50",
        transaction_date=today,
        description="gasolina",
    )
    t(user=alice, category=food, amount="50", transaction_date=today, description="mercado")
    t(user=bob, category=transport, subcategory=fuel, amount="50", transaction_date=today)
    since = today - timedelta(days=90)

    assert selectors.find_transactions(user=alice, since=since, subcategory=fuel) == [gas1]
    assert selectors.find_transactions(user=alice, since=since, description="GASO") == [gas1]
    assert len(selectors.find_transactions(user=alice, since=since, amount=Decimal("50"))) == 2
    assert len(selectors.find_transactions(user=alice, since=since, limit=1)) == 1
    soft_delete_transaction(actor=alice, transaction_id=gas1.pk)
    assert selectors.find_transactions(user=alice, since=since, subcategory=fuel) == []
