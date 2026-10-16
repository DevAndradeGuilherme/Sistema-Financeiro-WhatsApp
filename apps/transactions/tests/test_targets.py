"""Domain pieces behind UPDATE/UNDO target selection (closed-beta integrity fixes)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.families.services import add_family_member, create_family
from apps.transactions import selectors
from apps.transactions.models import Transaction, TransactionChange
from apps.transactions.services import (
    soft_delete_transaction,
    soft_delete_transactions,
    undo_last_change,
)

pytestmark = pytest.mark.django_db
Action = TransactionChange.Action


@pytest.fixture
def rows(alice, bob, food, salary, make_transaction):
    t = make_transaction
    return {
        "old_income": t(
            user=alice,
            type="INCOME",
            category=salary,
            amount="50",
            transaction_date=date(2026, 3, 1),
        ),
        "income": t(
            user=alice,
            type="INCOME",
            category=salary,
            amount="50",
            transaction_date=date(2026, 10, 7),
        ),
        "expense": t(user=alice, category=food, amount="34", transaction_date=date(2026, 10, 7)),
        "bobs": t(
            user=bob,
            type="INCOME",
            category=salary,
            amount="50",
            transaction_date=date(2026, 10, 7),
        ),
    }


def test_find_applies_every_filter_and_period(alice, rows):
    find = selectors.find_transactions
    assert find(user=alice, type="INCOME", amount=Decimal("50"), limit=5) == [
        rows["income"],
        rows["old_income"],
    ]
    assert find(user=alice, type="INCOME", transaction_date=date(2026, 10, 7)) == [rows["income"]]
    assert find(user=alice, start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), limit=5) == [
        rows["expense"],
        rows["income"],
    ]
    assert find(user=alice, since=date(2026, 9, 1), type="INCOME") == [rows["income"]]
    with pytest.raises(ValidationError):
        find(user=alice, start_date=date(2026, 10, 2), end_date=date(2026, 10, 1))


def test_find_orders_by_registration_not_transaction_date(alice, food, make_transaction):
    late = make_transaction(user=alice, category=food, transaction_date=date(2026, 9, 1))
    assert selectors.find_transactions(user=alice, limit=1)[0] == late


def test_summary_counts_only_own_active(alice, rows):
    ids = [t.pk for t in rows.values()]
    rows["expense"].deleted_at = timezone.now()
    rows["expense"].save()
    summary = selectors.summarize_own_transactions(user=alice, transaction_ids=ids)
    assert (summary.count, summary.total) == (2, Decimal("100.00"))  # Bob's and deleted excluded


def test_soft_delete_many_is_all_or_nothing(alice, rows):
    with pytest.raises(PermissionDenied):
        soft_delete_transactions(actor=alice, transaction_ids=[rows["income"].pk, rows["bobs"].pk])
    assert not Transaction.objects.deleted().exists()
    assert not TransactionChange.objects.filter(action=Action.DELETE).exists()

    deleted = soft_delete_transactions(
        actor=alice, transaction_ids=[rows["income"].pk, rows["expense"].pk], origin="whatsapp:x:1"
    )
    assert {t.pk for t in deleted} == {rows["income"].pk, rows["expense"].pk}
    changes = TransactionChange.objects.filter(action=Action.DELETE)
    assert {c.origin for c in changes} == {"whatsapp:x:1"} and changes.count() == 2
    with pytest.raises(PermissionDenied):  # already deleted → nothing again
        soft_delete_transactions(actor=alice, transaction_ids=[rows["income"].pk])
    with pytest.raises(ValidationError):
        soft_delete_transactions(actor=alice, transaction_ids=[])


def test_admin_cannot_soft_delete_member_transactions(alice, bob, rows):
    family = create_family(name="F", admin=alice)
    add_family_member(family=family, user=bob)
    with pytest.raises(PermissionDenied):
        soft_delete_transactions(actor=alice, transaction_ids=[rows["bobs"].pk])
    with pytest.raises(PermissionDenied):
        soft_delete_transaction(actor=alice, transaction_id=rows["bobs"].pk)
    assert rows["bobs"].__class__.objects.get(pk=rows["bobs"].pk).deleted_at is None


def test_targeted_delete_is_skipped_by_undo_stack(alice, rows):
    soft_delete_transaction(actor=alice, transaction_id=rows["expense"].pk, origin="o")
    result = undo_last_change(user=alice)  # DELETE is not reversible; deleted rows are skipped
    assert result.transaction == rows["income"]


def test_last_change_is_own_and_recent(alice, bob, rows):
    since = timezone.now() - timedelta(minutes=5)
    assert selectors.last_change(user=alice, since=since).transaction == rows["expense"]
    assert selectors.last_change(user=bob, since=since).transaction == rows["bobs"]
    TransactionChange.objects.update(created_at=timezone.now() - timedelta(minutes=10))
    assert selectors.last_change(user=alice, since=since) is None
