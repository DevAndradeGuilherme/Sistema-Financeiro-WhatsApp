"""V1.2: authorized read scopes and family selectors (domain level)."""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.families.models import FamilyMember
from apps.families.services import add_family_member, create_family
from apps.transactions import scopes, selectors
from apps.transactions.scopes import ReadScope, ScopeDenied
from apps.transactions.services import (
    soft_delete_transaction,
    undo_last_change,
    update_transaction,
)

pytestmark = pytest.mark.django_db

START, END = date(2026, 10, 1), date(2026, 10, 31)


@pytest.fixture
def people(make_user):
    return {
        "admin": make_user(username="gui", first_name="Guilherme", phone_number="+5531900000001"),
        "maria": make_user(
            username="maria1", first_name="Maria", last_name="Silva", phone_number="+5531900000002"
        ),
        "joao": make_user(username="joao", first_name="João", phone_number="+5531900000003"),
        "other_admin": make_user(username="bob", first_name="Bob", phone_number="+5531900000004"),
        "other_maria": make_user(
            username="maria2", first_name="Maria", last_name="Souza", phone_number="+5531900000005"
        ),
        "loner": make_user(username="solo", first_name="Solo", phone_number="+5531900000006"),
    }


@pytest.fixture
def families(people):
    family_a = create_family(name="A", admin=people["admin"])
    add_family_member(family=family_a, user=people["maria"])
    add_family_member(family=family_a, user=people["joao"])
    family_b = create_family(name="B", admin=people["other_admin"])
    add_family_member(family=family_b, user=people["other_maria"])
    return family_a, family_b


@pytest.fixture
def ledger(people, families, transport, food, salary, make_transaction):
    t = make_transaction
    p = people
    t(user=p["admin"], category=transport, amount="800.00", transaction_date=date(2026, 10, 2))
    t(
        user=p["maria"],
        category=food,
        amount="420.00",
        transaction_date=date(2026, 10, 3),
        description="mercado",
    )
    t(user=p["maria"], category=transport, amount="200.00", transaction_date=date(2026, 10, 4))
    t(user=p["joao"], category=food, amount="310.00", transaction_date=date(2026, 10, 5))
    t(
        user=p["maria"],
        category=salary,
        type="INCOME",
        amount="3000.00",
        transaction_date=date(2026, 10, 5),
    )
    t(user=p["joao"], category=food, amount="99.00", transaction_date=date(2026, 9, 10))
    # Other family: never visible to family A.
    t(user=p["other_admin"], category=food, amount="7777.00", transaction_date=date(2026, 10, 2))
    t(user=p["other_maria"], category=food, amount="5555.00", transaction_date=date(2026, 10, 2))


# --- Construction / authorization --------------------------------------------------------


def test_read_scope_cannot_be_built_directly(people):
    with pytest.raises(TypeError):
        ReadScope(scopes.FAMILY, people["maria"])


def test_member_cannot_get_family_scope(people, families):
    with pytest.raises(ScopeDenied) as exc:
        scopes.family(people["maria"])
    assert exc.value.code == ScopeDenied.ADMIN_ONLY


def test_member_cannot_get_family_member_scope_and_names_are_not_checked(people, families):
    with pytest.raises(ScopeDenied) as exc:
        scopes.family_member(people["maria"], "Ninguém")
    assert exc.value.code == ScopeDenied.ADMIN_ONLY  # authorization before name resolution


def test_user_without_family(people):
    with pytest.raises(ScopeDenied) as exc:
        scopes.family(people["loner"])
    assert exc.value.code == ScopeDenied.NO_FAMILY


def test_resolve_defaults_to_self(people, families):
    assert scopes.resolve(people["maria"], None).kind == scopes.SELF
    assert scopes.resolve(people["maria"], "ANYTHING").kind == scopes.SELF


def test_lost_admin_role_is_rechecked(people, families):
    family_a, _ = families
    assert scopes.family(people["admin"]).family == family_a
    FamilyMember.objects.filter(user=people["admin"]).update(role="MEMBER")
    with pytest.raises(ScopeDenied):
        scopes.family(people["admin"])


# --- Member resolution ---------------------------------------------------------------


def test_member_resolved_by_first_name_accent_insensitive(people, families):
    assert scopes.family_member(people["admin"], "joao").member == people["joao"]
    assert scopes.family_member(people["admin"], "JOÃO").member == people["joao"]


def test_member_from_other_family_does_not_resolve(people, families):
    with pytest.raises(ScopeDenied) as exc:
        scopes.family_member(people["admin"], "Bob")
    assert exc.value.code == ScopeDenied.MEMBER_NOT_FOUND


def test_phone_or_id_are_not_names(people, families):
    for hint in ("+5531900000002", "5531900000002", str(people["maria"].pk), ""):
        with pytest.raises(ScopeDenied):
            scopes.family_member(people["admin"], hint)


def test_ambiguous_first_name_is_not_chosen(people, families, make_user):
    family_a, _ = families
    other = make_user(
        username="maria3", first_name="Maria", last_name="Costa", phone_number="+5531900000007"
    )
    add_family_member(family=family_a, user=other)
    with pytest.raises(ScopeDenied) as exc:
        scopes.family_member(people["admin"], "Maria")
    assert exc.value.code == ScopeDenied.MEMBER_AMBIGUOUS
    assert scopes.family_member(people["admin"], "maria silva").member == people["maria"]


# --- Family selectors --------------------------------------------------------------------


def test_family_totals_and_balance(people, ledger):
    scope = scopes.family(people["admin"])
    assert selectors.get_total(scope=scope, start_date=START, end_date=END, type="EXPENSE") == (
        Decimal("1730.00")
    )
    totals = selectors.get_period_totals(scope=scope, start_date=START, end_date=END)
    assert (totals.income, totals.expense, totals.balance) == (
        Decimal("3000.00"),
        Decimal("1730.00"),
        Decimal("1270.00"),
    )


def test_family_breakdown_top_compare(people, ledger, food):
    scope = scopes.family(people["admin"])
    rows = selectors.get_totals_by_category(
        scope=scope, start_date=START, end_date=END, type="EXPENSE"
    )
    assert [(r.category_name, r.total) for r in rows] == [
        ("Transporte", Decimal("1000.00")),
        ("Alimentação", Decimal("730.00")),
    ]
    top = list(selectors.get_top_expenses(scope=scope, start_date=START, end_date=END, limit=2))
    assert [t.amount for t in top] == [Decimal("800.00"), Decimal("420.00")]
    comparison = selectors.compare_periods(
        scope=scope,
        type="EXPENSE",
        start_date=START,
        end_date=END,
        compare_start_date=date(2026, 9, 1),
        compare_end_date=date(2026, 9, 30),
    )
    assert (comparison.current, comparison.previous) == (Decimal("1730.00"), Decimal("99.00"))


def test_member_ranking_aggregated_in_database(people, ledger):
    scope = scopes.family(people["admin"])
    rows = selectors.get_member_totals(scope=scope, start_date=START, end_date=END, type="EXPENSE")
    assert [(r.name, r.total) for r in rows] == [
        ("Guilherme", Decimal("800.00")),
        ("Maria Silva", Decimal("620.00")),
        ("João", Decimal("310.00")),
    ]


def test_member_ranking_query_count_does_not_grow_with_members(
    people, ledger, make_user, food, make_transaction
):
    scope = scopes.family(people["admin"])
    with CaptureQueriesContext(connection) as before:
        selectors.get_member_totals(scope=scope, start_date=START, end_date=END, type="EXPENSE")
    family_a = scope.family
    for index in range(5):
        member = make_user(username=f"extra{index}", phone_number=f"+55319000001{index:02d}")
        add_family_member(family=family_a, user=member)
        make_transaction(user=member, category=food, amount="1", transaction_date=START)
    with CaptureQueriesContext(connection) as after:
        rows = selectors.get_member_totals(
            scope=scope, start_date=START, end_date=END, type="EXPENSE"
        )
    assert len(rows) == 8
    assert len(after.captured_queries) == len(before.captured_queries) == 1  # one GROUP BY


def test_member_ranking_requires_family_scope(people, ledger):
    with pytest.raises(TypeError):
        selectors.get_member_totals(
            scope=scopes.own(people["admin"]), start_date=START, end_date=END, type="EXPENSE"
        )


def test_family_member_scope(people, ledger, food):
    scope = scopes.family_member(people["admin"], "Maria")
    assert selectors.get_total(scope=scope, start_date=START, end_date=END, type="EXPENSE") == (
        Decimal("620.00")
    )
    assert selectors.get_total(
        scope=scope, start_date=START, end_date=END, type="EXPENSE", category=food
    ) == Decimal("420.00")


def test_self_scope_never_includes_other_members(people, ledger):
    scope = scopes.own(people["maria"])
    assert selectors.get_total(scope=scope, start_date=START, end_date=END, type="EXPENSE") == (
        Decimal("620.00")
    )


def test_family_a_never_sees_family_b(people, ledger):
    scope = scopes.family(people["admin"])
    ids = {t.user_id for t in selectors.list_transactions(scope=scope)}
    assert people["other_admin"].pk not in ids
    assert people["other_maria"].pk not in ids


# --- Historical snapshot -------------------------------------------------------------


def test_transactions_before_joining_are_not_family_data(people, families, food, make_transaction):
    family_a, _ = families
    newcomer = people["loner"]
    old = make_transaction(user=newcomer, category=food, amount="50", transaction_date=START)
    assert old.family is None
    add_family_member(family=family_a, user=newcomer)
    new = make_transaction(user=newcomer, category=food, amount="7", transaction_date=START)

    family_scope = scopes.family(people["admin"])
    visible = {t.pk for t in selectors.list_transactions(scope=family_scope)}
    assert new.pk in visible
    assert old.pk not in visible  # current membership does not grant retroactive access
    member_scope = scopes.family_member(people["admin"], "Solo")
    assert selectors.get_total(
        scope=member_scope, start_date=START, end_date=END, type="EXPENSE"
    ) == Decimal("7.00")


def test_member_who_left_keeps_history_in_family_snapshot_only(
    people, families, food, make_transaction
):
    family_a, family_b = families
    txn = make_transaction(user=people["joao"], category=food, amount="10", transaction_date=START)
    FamilyMember.objects.filter(user=people["joao"]).delete()
    add_family_member(family=family_b, user=people["joao"])
    later = make_transaction(
        user=people["joao"], category=food, amount="20", transaction_date=START
    )

    a_ids = {t.pk for t in selectors.list_transactions(scope=scopes.family(people["admin"]))}
    b_ids = {t.pk for t in selectors.list_transactions(scope=scopes.family(people["other_admin"]))}
    assert txn.pk in a_ids and later.pk not in a_ids  # history stays where it was recorded
    assert later.pk in b_ids and txn.pk not in b_ids  # new family does not get the old history
    with pytest.raises(ScopeDenied):  # no longer a member: cannot be targeted by name in A
        scopes.family_member(people["admin"], "João")


# --- Mutations stay owner-only ---------------------------------------------------------


def test_admin_cannot_mutate_member_data(people, families, food, make_transaction):
    txn = make_transaction(user=people["maria"], category=food, amount="50")
    with pytest.raises(PermissionDenied):
        update_transaction(user=people["admin"], transaction_id=txn.pk, amount="30")
    with pytest.raises(PermissionDenied):
        soft_delete_transaction(actor=people["admin"], transaction_id=txn.pk)
    assert undo_last_change(user=people["admin"]) is None  # admin has no actions of its own
    txn.refresh_from_db()
    assert (txn.amount, txn.deleted_at) == (Decimal("50.00"), None)
