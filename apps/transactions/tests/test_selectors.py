from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.families.services import add_family_member, create_family
from apps.transactions import selectors
from apps.transactions.models import TransactionType
from apps.transactions.services import soft_delete_transaction

pytestmark = pytest.mark.django_db

OCT_1 = date(2026, 10, 1)
OCT_31 = date(2026, 10, 31)
EXPENSE = TransactionType.EXPENSE
INCOME = TransactionType.INCOME


@pytest.fixture
def ledger(alice, bob, transport, fuel, food, salary, make_transaction):
    """Alice: October expenses/income + one September expense. Bob: his own data."""
    t = make_transaction
    return {
        "fuel": t(
            user=alice,
            category=transport,
            subcategory=fuel,
            amount="50.10",
            transaction_date=date(2026, 10, 2),
        ),
        "bus": t(user=alice, category=transport, amount="4.50", transaction_date=date(2026, 10, 5)),
        "lunch": t(user=alice, category=food, amount="32.40", transaction_date=date(2026, 10, 3)),
        "market": t(user=alice, category=food, amount="210.00", transaction_date=OCT_1),
        "september": t(
            user=alice, category=food, amount="99.99", transaction_date=date(2026, 9, 30)
        ),
        "salary": t(
            user=alice,
            category=salary,
            type=INCOME,
            amount="3000.00",
            transaction_date=date(2026, 10, 5),
        ),
        "bob_fuel": t(
            user=bob, category=transport, amount="999.00", transaction_date=date(2026, 10, 2)
        ),
        "bob_salary": t(
            user=bob,
            category=salary,
            type=INCOME,
            amount="8000.00",
            transaction_date=date(2026, 10, 5),
        ),
    }


def test_list_transactions_of_user(alice, ledger):
    result = list(selectors.list_transactions(user=alice))
    assert len(result) == 6
    assert result[0].transaction_date >= result[-1].transaction_date
    assert all(t.user_id == alice.pk for t in result)


def test_expenses_and_incomes_in_period(alice, ledger):
    expenses = selectors.list_transactions(
        user=alice, start_date=OCT_1, end_date=OCT_31, type=EXPENSE
    )
    incomes = selectors.list_transactions(
        user=alice, start_date=OCT_1, end_date=OCT_31, type=INCOME
    )
    assert {t.pk for t in expenses} == {ledger[k].pk for k in ("fuel", "bus", "lunch", "market")}
    assert [t.pk for t in incomes] == [ledger["salary"].pk]


def test_period_is_inclusive(alice, ledger):
    result = selectors.list_transactions(user=alice, start_date=OCT_1, end_date=OCT_1)
    assert [t.pk for t in result] == [ledger["market"].pk]


def test_total_by_period(alice, ledger):
    total = selectors.get_total(user=alice, start_date=OCT_1, end_date=OCT_31, type=EXPENSE)
    assert total == Decimal("297.00")
    assert isinstance(total, Decimal)


def test_period_totals_and_balance(alice, ledger):
    totals = selectors.get_period_totals(user=alice, start_date=OCT_1, end_date=OCT_31)
    assert totals.income == Decimal("3000.00")
    assert totals.expense == Decimal("297.00")
    assert totals.balance == Decimal("2703.00")


def test_empty_period_returns_zero(alice):
    totals = selectors.get_period_totals(user=alice, start_date=OCT_1, end_date=OCT_31)
    assert totals.income == Decimal("0.00")
    assert totals.expense == Decimal("0.00")
    assert selectors.get_total(user=alice, start_date=OCT_1, end_date=OCT_31, type=EXPENSE) == 0


def test_total_by_category_and_subcategory(alice, ledger, transport, fuel):
    by_category = selectors.get_total(
        user=alice, start_date=OCT_1, end_date=OCT_31, type=EXPENSE, category=transport
    )
    by_subcategory = selectors.get_total(
        user=alice, start_date=OCT_1, end_date=OCT_31, type=EXPENSE, subcategory=fuel
    )
    assert by_category == Decimal("54.60")
    assert by_subcategory == Decimal("50.10")


def test_totals_grouped_by_category(alice, ledger, food, transport):
    rows = selectors.get_totals_by_category(
        user=alice, start_date=OCT_1, end_date=OCT_31, type=EXPENSE
    )
    assert [(r.category_id, r.total) for r in rows] == [
        (food.pk, Decimal("242.40")),
        (transport.pk, Decimal("54.60")),
    ]


def test_top_expenses(alice, ledger):
    top = list(selectors.get_top_expenses(user=alice, start_date=OCT_1, end_date=OCT_31, limit=2))
    assert [t.pk for t in top] == [ledger["market"].pk, ledger["fuel"].pk]


def test_top_expenses_limit_validated(alice):
    with pytest.raises(ValidationError):
        selectors.get_top_expenses(user=alice, start_date=OCT_1, end_date=OCT_31, limit=0)
    with pytest.raises(ValidationError):
        selectors.get_top_expenses(user=alice, start_date=OCT_1, end_date=OCT_31, limit=1000)


def test_invalid_period_rejected(alice):
    with pytest.raises(ValidationError):
        selectors.get_total(user=alice, start_date=OCT_31, end_date=OCT_1, type=EXPENSE)
    with pytest.raises(ValidationError):
        selectors.get_total(user=alice, start_date=OCT_1, end_date=OCT_31, type="TRANSFER")


def test_soft_deleted_excluded_from_all_selectors(alice, ledger):
    soft_delete_transaction(actor=alice, transaction_id=ledger["market"].pk)

    assert ledger["market"].pk not in {t.pk for t in selectors.list_transactions(user=alice)}
    assert selectors.get_total(
        user=alice, start_date=OCT_1, end_date=OCT_31, type=EXPENSE
    ) == Decimal("87.00")
    assert selectors.get_period_totals(
        user=alice, start_date=OCT_1, end_date=OCT_31
    ).expense == Decimal("87.00")
    top = selectors.get_top_expenses(user=alice, start_date=OCT_1, end_date=OCT_31)
    assert ledger["market"].pk not in {t.pk for t in top}
    rows = selectors.get_totals_by_category(
        user=alice, start_date=OCT_1, end_date=OCT_31, type=EXPENSE
    )
    assert sum(r.total for r in rows) == Decimal("87.00")


# --- Isolation between users (mandatory) -------------------------------------


def test_selectors_never_return_other_users_data(alice, bob, ledger):
    bob_ids = {ledger["bob_fuel"].pk, ledger["bob_salary"].pk}

    assert not bob_ids & {t.pk for t in selectors.list_transactions(user=alice)}
    top = selectors.get_top_expenses(user=alice, start_date=OCT_1, end_date=OCT_31, limit=50)
    assert not bob_ids & {t.pk for t in top}
    totals = selectors.get_period_totals(user=alice, start_date=OCT_1, end_date=OCT_31)
    assert totals.income == Decimal("3000.00")  # Bob's 8000 not included
    assert totals.expense == Decimal("297.00")  # Bob's 999 not included

    bob_totals = selectors.get_period_totals(user=bob, start_date=OCT_1, end_date=OCT_31)
    assert bob_totals.income == Decimal("8000.00")
    assert bob_totals.expense == Decimal("999.00")


def test_family_membership_does_not_widen_selector_scope(alice, bob, ledger):
    family = create_family(name="Silva", admin=alice)
    add_family_member(family=family, user=bob)

    # Even as Family ADMIN, per-user selectors only return the given user's data.
    alice_ids = {t.pk for t in selectors.list_transactions(user=alice)}
    assert ledger["bob_fuel"].pk not in alice_ids
    bob_ids = {t.pk for t in selectors.list_transactions(user=bob)}
    assert ledger["fuel"].pk not in bob_ids


def test_selectors_require_saved_user():
    with pytest.raises(TypeError):
        selectors.list_transactions(user=None)
