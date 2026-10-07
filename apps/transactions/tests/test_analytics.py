"""V1.5 analytics (domain): summary, monthly trend, category trend — Decimal, server-side."""

from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.families.services import add_family_member, create_family
from apps.transactions import analytics, scopes
from apps.transactions.analytics import (
    CategoryChange,
    MonthlyPoint,
    MonthlyTrend,
    PeriodSummary,
    month_window,
)
from apps.transactions.services import (
    soft_delete_transaction,
    undo_last_change,
    update_transaction,
)
from common.money import percent

pytestmark = pytest.mark.django_db

OCT = (date(2026, 10, 1), date(2026, 10, 31))
SEP = (date(2026, 9, 1), date(2026, 9, 30))


def summary(user, period=OCT):
    return analytics.period_summary(
        scope=scopes.own(user), start_date=period[0], end_date=period[1]
    )


@pytest.fixture
def ledger(alice, transport, food, salary, make_transaction):
    t = make_transaction
    return {
        "salary": t(
            user=alice,
            category=salary,
            type="INCOME",
            amount="3000.00",
            transaction_date=date(2026, 10, 5),
            description="salário",
        ),
        "freela": t(
            user=alice,
            category=salary,
            type="INCOME",
            amount="500.00",
            transaction_date=date(2026, 10, 6),
        ),
        "market": t(
            user=alice,
            category=food,
            amount="350.00",
            description="mercado",
            transaction_date=date(2026, 10, 2),
        ),
        "lunch": t(user=alice, category=food, amount="370.00", transaction_date=date(2026, 10, 3)),
        "fuel": t(
            user=alice, category=transport, amount="280.00", transaction_date=date(2026, 10, 4)
        ),
        "sep_food": t(
            user=alice, category=food, amount="600.00", transaction_date=date(2026, 9, 10)
        ),
        "sep_fuel": t(
            user=alice, category=transport, amount="300.00", transaction_date=date(2026, 9, 11)
        ),
    }


# --- PERIOD_SUMMARY ---------------------------------------------------------------------------


def test_period_summary_metrics(alice, ledger, food, transport):
    s = summary(alice)
    assert (s.income_total, s.expense_total, s.balance) == (
        Decimal("3500.00"),
        Decimal("1000.00"),
        Decimal("2500.00"),
    )
    assert (s.income_count, s.expense_count) == (2, 3)
    assert s.average_expense == Decimal("333.33")
    assert (s.largest_expense.label, s.largest_expense.amount) == ("Alimentação", Decimal("370.00"))
    assert (s.largest_income.label, s.largest_income.amount) == ("Salário", Decimal("3000.00"))
    assert [(c.category_name, c.total) for c in s.top_categories] == [
        ("Alimentação", Decimal("720.00")),
        ("Transporte", Decimal("280.00")),
    ]
    assert s.expense_ratio == Decimal("28.6")  # 1000/3500
    assert s.savings_rate == Decimal("71.4")
    for value in (s.income_total, s.average_expense, s.expense_ratio, s.savings_rate):
        assert isinstance(value, Decimal)


def test_summary_without_income_has_no_percentages(alice, food, make_transaction):
    make_transaction(user=alice, category=food, amount="10", transaction_date=date(2026, 10, 2))
    s = summary(alice)
    assert (s.income_total, s.expense_ratio, s.savings_rate, s.largest_income) == (
        Decimal("0.00"),
        None,
        None,
        None,
    )
    assert s.balance == Decimal("-10.00")


def test_summary_without_expenses(alice, salary, make_transaction):
    make_transaction(
        user=alice, category=salary, type="INCOME", amount="100", transaction_date=date(2026, 10, 2)
    )
    s = summary(alice)
    assert (s.expense_ratio, s.savings_rate, s.average_expense, s.largest_expense) == (
        Decimal("0.0"),
        Decimal("100.0"),
        None,
        None,
    )


def test_negative_balance_and_ratio_above_100(alice, food, salary, make_transaction):
    make_transaction(
        user=alice,
        category=salary,
        type="INCOME",
        amount="1000",
        transaction_date=date(2026, 10, 2),
    )
    make_transaction(user=alice, category=food, amount="1300", transaction_date=date(2026, 10, 3))
    s = summary(alice)
    assert s.balance == Decimal("-300.00")
    assert s.expense_ratio == Decimal("130.0")
    assert s.savings_rate == Decimal("-30.0")


def test_income_equals_expenses(alice, food, salary, make_transaction):
    make_transaction(
        user=alice, category=salary, type="INCOME", amount="500", transaction_date=date(2026, 10, 2)
    )
    make_transaction(user=alice, category=food, amount="500", transaction_date=date(2026, 10, 3))
    s = summary(alice)
    assert (s.balance, s.expense_ratio, s.savings_rate) == (
        Decimal("0.00"),
        Decimal("100.0"),
        Decimal("0.0"),
    )


def test_empty_period(alice):
    s = summary(alice)
    assert not s.has_data
    assert (s.income_total, s.expense_total, s.expense_ratio) == (
        Decimal("0.00"),
        Decimal("0.00"),
        None,
    )


def test_summary_reflects_soft_delete_update_and_undo(alice, ledger):
    soft_delete_transaction(actor=alice, transaction_id=ledger["market"].pk)
    assert summary(alice).expense_total == Decimal("650.00")
    update_transaction(user=alice, transaction_id=ledger["fuel"].pk, amount="80")
    assert summary(alice).expense_total == Decimal("450.00")
    undo_last_change(user=alice)  # undoes the update
    assert summary(alice).expense_total == Decimal("650.00")


def test_rounding_of_cents():
    s = PeriodSummary(Decimal("3.00"), Decimal("1.00"), 1, 3, None, None)
    assert s.average_expense == Decimal("0.33")
    assert s.expense_ratio == Decimal("33.3")
    assert percent(Decimal("2"), Decimal("3")) == Decimal("66.7")  # ROUND_HALF_UP
    assert percent(Decimal("1"), Decimal("0")) is None


# --- MONTHLY_TREND ------------------------------------------------------------------------------


def test_month_window_includes_current_month_and_crosses_year():
    assert month_window(3, date(2026, 10, 7)) == [
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 10, 1),
    ]
    assert month_window(6, date(2026, 2, 15)) == [
        date(2025, 9, 1),
        date(2025, 10, 1),
        date(2025, 11, 1),
        date(2025, 12, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]
    assert len(month_window(12, date(2026, 10, 7))) == 12
    for invalid in (0, -1, 13, 50):
        with pytest.raises(ValueError):
            month_window(invalid, date(2026, 10, 7))


def test_monthly_trend_fills_empty_months_with_zero(alice, food, make_transaction):
    make_transaction(user=alice, category=food, amount="100", transaction_date=date(2026, 9, 3))
    make_transaction(user=alice, category=food, amount="200", transaction_date=date(2026, 10, 3))
    trend = analytics.monthly_trend(scope=scopes.own(alice), months=3, end_month=date(2026, 10, 7))
    assert [(p.month, p.expense) for p in trend.points] == [
        (date(2026, 8, 1), Decimal("0.00")),
        (date(2026, 9, 1), Decimal("100.00")),
        (date(2026, 10, 1), Decimal("200.00")),
    ]
    assert trend.average == Decimal("100.00")  # empty month counts as zero (not 150)


def test_monthly_trend_metrics(alice, ledger):
    scope = scopes.own(alice)
    expense = analytics.monthly_trend(scope=scope, months=2, end_month=date(2026, 10, 1))
    income = analytics.monthly_trend(
        scope=scope, months=2, end_month=date(2026, 10, 1), metric="INCOME"
    )
    balance = analytics.monthly_trend(
        scope=scope, months=2, end_month=date(2026, 10, 1), metric="BALANCE"
    )
    assert [p.value(expense.metric) for p in expense.points] == [
        Decimal("900.00"),
        Decimal("1000.00"),
    ]
    assert [p.value(income.metric) for p in income.points] == [Decimal("0.00"), Decimal("3500.00")]
    assert [p.value(balance.metric) for p in balance.points] == [
        Decimal("-900.00"),
        Decimal("2500.00"),
    ]
    assert balance.average == Decimal("800.00")


def test_monthly_trend_crossing_year(alice, food, make_transaction):
    make_transaction(user=alice, category=food, amount="10", transaction_date=date(2025, 12, 31))
    make_transaction(user=alice, category=food, amount="20", transaction_date=date(2026, 1, 1))
    trend = analytics.monthly_trend(scope=scopes.own(alice), months=2, end_month=date(2026, 1, 10))
    assert [(p.month, p.expense) for p in trend.points] == [
        (date(2025, 12, 1), Decimal("10.00")),
        (date(2026, 1, 1), Decimal("20.00")),
    ]


def test_monthly_trend_is_one_query_regardless_of_months(alice, ledger):
    scope = scopes.own(alice)
    with CaptureQueriesContext(connection) as three:
        analytics.monthly_trend(scope=scope, months=3, end_month=date(2026, 10, 1))
    with CaptureQueriesContext(connection) as twelve:
        analytics.monthly_trend(scope=scope, months=12, end_month=date(2026, 10, 1))
    assert len(three.captured_queries) == len(twelve.captured_queries) == 1
    assert "date_trunc" in twelve.captured_queries[0]["sql"].lower()


def test_trend_dto_without_data():
    trend = MonthlyTrend(
        points=(MonthlyPoint(date(2026, 10, 1), Decimal("0"), Decimal("0")),), metric="EXPENSE"
    )
    assert not trend.has_data and trend.average == Decimal("0.00")


# --- CATEGORY_TREND -----------------------------------------------------------------------------


def changes(user, direction="ALL", **kw):
    return analytics.category_trend(
        scope=scopes.own(user),
        type="EXPENSE",
        start_date=OCT[0],
        end_date=OCT[1],
        compare_start_date=SEP[0],
        compare_end_date=SEP[1],
        direction=direction,
        **kw,
    )


@pytest.fixture
def trend_ledger(alice, transport, food, make_transaction):
    from apps.categories.models import Category

    leisure = Category.objects.create(name="Lazer", slug="lazer", kind="EXPENSE")
    health = Category.objects.create(name="Saúde", slug="saude", kind="EXPENSE")
    bills = Category.objects.create(name="Contas", slug="contas", kind="EXPENSE")
    t = make_transaction
    t(user=alice, category=food, amount="620.00", transaction_date=date(2026, 10, 2))  # +120
    t(user=alice, category=food, amount="500.00", transaction_date=date(2026, 9, 2))
    t(user=alice, category=transport, amount="200.00", transaction_date=date(2026, 10, 2))  # -100
    t(user=alice, category=transport, amount="300.00", transaction_date=date(2026, 9, 2))
    t(user=alice, category=leisure, amount="80.00", transaction_date=date(2026, 10, 2))  # new
    t(user=alice, category=health, amount="90.00", transaction_date=date(2026, 9, 2))  # gone
    t(user=alice, category=bills, amount="150.00", transaction_date=date(2026, 10, 2))  # equal
    t(user=alice, category=bills, amount="150.00", transaction_date=date(2026, 9, 2))
    return {"leisure": leisure, "health": health, "bills": bills}


def test_category_trend_increase(alice, trend_ledger):
    result = changes(alice, "INCREASE")
    assert [(c.category_name, c.change, c.percent) for c in result] == [
        ("Alimentação", Decimal("120.00"), Decimal("24.0")),
        ("Lazer", Decimal("80.00"), None),  # previous = 0: no percentage
    ]


def test_category_trend_decrease(alice, trend_ledger):
    result = changes(alice, "DECREASE")
    assert [(c.category_name, c.change, c.percent) for c in result] == [
        ("Transporte", Decimal("-100.00"), Decimal("-33.3")),
        ("Saúde", Decimal("-90.00"), Decimal("-100.0")),  # current = 0
    ]


def test_category_trend_all_sorted_by_absolute_change(alice, trend_ledger):
    names = [c.category_name for c in changes(alice)]
    assert names == ["Alimentação", "Transporte", "Saúde", "Lazer", "Contas"]
    equal = next(c for c in changes(alice) if c.category_name == "Contas")
    assert (equal.change, equal.percent) == (Decimal("0.00"), Decimal("0.0"))


def test_category_trend_limit_and_single_category(alice, trend_ledger, food):
    assert len(changes(alice, limit=2)) == 2
    only = changes(alice, category_id=food.pk)
    assert [c.category_name for c in only] == ["Alimentação"]


def test_category_trend_ignores_soft_deleted(alice, trend_ledger, food, make_transaction):
    extra = make_transaction(
        user=alice, category=food, amount="1000", transaction_date=date(2026, 10, 5)
    )
    soft_delete_transaction(actor=alice, transaction_id=extra.pk)
    food_change = next(c for c in changes(alice) if c.category_name == "Alimentação")
    assert food_change.current == Decimal("620.00")


def test_category_trend_single_query(alice, trend_ledger):
    with CaptureQueriesContext(connection) as ctx:
        changes(alice)
    assert len(ctx.captured_queries) == 1


def test_category_change_dto():
    assert CategoryChange(1, "X", Decimal("0"), Decimal("0")).percent is None


# --- FAMILY / FAMILY_MEMBER -----------------------------------------------------------------------


@pytest.fixture
def family_ledger(make_user, food, transport, salary, make_transaction):
    admin = make_user(username="gui", first_name="Guilherme", phone_number="+5531900000101")
    maria = make_user(username="maria", first_name="Maria", phone_number="+5531900000102")
    joao = make_user(username="joao", first_name="João", phone_number="+5531900000103")
    outsider = make_user(username="bob", first_name="Bob", phone_number="+5531900000104")
    old = make_transaction(
        user=maria, category=food, amount="999.00", transaction_date=date(2026, 10, 1)
    )  # before joining: not family data
    family = create_family(name="F", admin=admin)
    add_family_member(family=family, user=maria)
    add_family_member(family=family, user=joao)
    create_family(name="Outra", admin=outsider)
    t = make_transaction
    t(user=admin, category=salary, type="INCOME", amount="4000", transaction_date=date(2026, 10, 2))
    t(user=admin, category=food, amount="800", transaction_date=date(2026, 10, 2))
    t(user=maria, category=food, amount="400", transaction_date=date(2026, 10, 3))
    t(user=maria, category=transport, amount="220", transaction_date=date(2026, 10, 4))
    t(user=joao, category=transport, amount="310", transaction_date=date(2026, 10, 5))
    t(user=maria, category=food, amount="100", transaction_date=date(2026, 9, 5))
    t(user=outsider, category=food, amount="7777", transaction_date=date(2026, 10, 2))
    return {"admin": admin, "maria": maria, "joao": joao, "outsider": outsider, "old": old}


def test_family_summary_uses_historical_family(family_ledger):
    scope = scopes.family(family_ledger["admin"])
    s = analytics.period_summary(scope=scope, start_date=OCT[0], end_date=OCT[1])
    assert (s.income_total, s.expense_total) == (Decimal("4000.00"), Decimal("1730.00"))
    assert s.expense_ratio == Decimal("43.3")


def test_family_trend_and_category_trend(family_ledger):
    scope = scopes.family(family_ledger["admin"])
    trend = analytics.monthly_trend(scope=scope, months=2, end_month=date(2026, 10, 9))
    assert [p.expense for p in trend.points] == [Decimal("100.00"), Decimal("1730.00")]
    result = analytics.category_trend(
        scope=scope,
        type="EXPENSE",
        start_date=OCT[0],
        end_date=OCT[1],
        compare_start_date=SEP[0],
        compare_end_date=SEP[1],
        direction="INCREASE",
    )
    assert [(c.category_name, c.change) for c in result] == [
        ("Alimentação", Decimal("1100.00")),
        ("Transporte", Decimal("530.00")),
    ]


def test_family_member_analytics(family_ledger):
    scope = scopes.family_member(family_ledger["admin"], "Maria")
    s = analytics.period_summary(scope=scope, start_date=OCT[0], end_date=OCT[1])
    assert s.expense_total == Decimal("620.00")  # the pre-family 999 is not visible
    trend = analytics.monthly_trend(scope=scope, months=2, end_month=date(2026, 10, 9))
    assert [p.expense for p in trend.points] == [Decimal("100.00"), Decimal("620.00")]


def test_member_cannot_get_family_analytics(family_ledger):
    with pytest.raises(scopes.ScopeDenied):
        scopes.family(family_ledger["maria"])
    with pytest.raises(scopes.ScopeDenied):
        scopes.family_member(family_ledger["admin"], "Bob")  # other family


def test_family_analytics_query_count_does_not_grow_with_members(
    family_ledger, make_user, food, make_transaction
):
    scope = scopes.family(family_ledger["admin"])

    def run():
        with CaptureQueriesContext(connection) as ctx:
            analytics.period_summary(scope=scope, start_date=OCT[0], end_date=OCT[1])
            analytics.monthly_trend(scope=scope, months=6, end_month=date(2026, 10, 9))
            analytics.category_trend(
                scope=scope,
                type="EXPENSE",
                start_date=OCT[0],
                end_date=OCT[1],
                compare_start_date=SEP[0],
                compare_end_date=SEP[1],
            )
        return len(ctx.captured_queries)

    before = run()
    for index in range(6):
        member = make_user(username=f"extra{index}", phone_number=f"+55319000002{index:02d}")
        add_family_member(family=scope.family, user=member)
        for month in (9, 10):
            make_transaction(
                user=member, category=food, amount="5", transaction_date=date(2026, month, 2)
            )
    assert run() == before  # no N+1 over members or transactions


def test_volume_is_aggregated_in_database(alice, food, transport, make_transaction):
    """Many rows over many months/categories: correct totals with a fixed number of queries."""
    for month in range(1, 13):  # a full past year: never future dates
        for day in (3, 13, 23):
            make_transaction(
                user=alice,
                category=food if day != 13 else transport,
                amount="10.10",
                transaction_date=date(2025, month, day),
            )
    scope = scopes.own(alice)
    with CaptureQueriesContext(connection) as ctx:
        trend = analytics.monthly_trend(scope=scope, months=12, end_month=date(2025, 12, 31))
    assert len(ctx.captured_queries) == 1
    assert sum(p.expense for p in trend.points) == Decimal("363.60")
    assert trend.average == Decimal("30.30")
