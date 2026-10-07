"""Deterministic financial analytics (V1.5). Read-only, computed on the current ledger.

Every function takes an authorized ``ReadScope`` (``apps.transactions.scopes``) — SELF,
FAMILY (historical ``Transaction.family``) or FAMILY_MEMBER — so analytics reuse the V1.2
authorization unchanged. Soft-deleted transactions are excluded (``scope.transactions()``
uses ``active()``); updated values are the current ones; ``TransactionChange``, inbound
messages and attachments are never read. TEXT/AUDIO/IMAGE transactions count the same.

All sums/counts/maxima/grouping run in PostgreSQL (Sum, Count, TruncMonth, conditional
aggregation); Python only combines a handful of aggregated rows. Money and percentages are
Decimal (no float anywhere): money quantized to 0.01, percentages to 0.1, ROUND_HALF_UP.
Nothing is persisted (no report tables, no snapshots).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth

from common.money import TWO_PLACES, percent

from .models import Transaction, TransactionType
from .scopes import ReadScope
from .selectors import CategoryTotal, get_top_expenses, get_totals_by_category

ZERO = Decimal("0.00")
MAX_MONTHS = 12
DEFAULT_TREND_MONTHS = 6
DEFAULT_TOP_CATEGORIES = 3
_MONEY = DecimalField(max_digits=14, decimal_places=2)
EXPENSE, INCOME = TransactionType.EXPENSE, TransactionType.INCOME


# --- DTOs (immutable, never persisted) --------------------------------------------------------


@dataclass(frozen=True)
class TransactionHighlight:
    label: str
    amount: Decimal


@dataclass(frozen=True)
class PeriodSummary:
    income_total: Decimal
    expense_total: Decimal
    income_count: int
    expense_count: int
    largest_expense: TransactionHighlight | None
    largest_income: TransactionHighlight | None
    top_categories: tuple[CategoryTotal, ...] = field(default_factory=tuple)

    @property
    def balance(self) -> Decimal:
        return self.income_total - self.expense_total

    @property
    def has_data(self) -> bool:
        return (self.income_count + self.expense_count) > 0

    @property
    def average_expense(self) -> Decimal | None:
        if not self.expense_count:
            return None
        return (self.expense_total / self.expense_count).quantize(TWO_PLACES, ROUND_HALF_UP)

    @property
    def expense_ratio(self) -> Decimal | None:
        """Expenses as % of income; None without income (never divides by zero)."""
        return percent(self.expense_total, self.income_total)

    @property
    def savings_rate(self) -> Decimal | None:
        """(income - expenses) / income * 100; may be negative; None without income."""
        return percent(self.balance, self.income_total)


@dataclass(frozen=True)
class MonthlyPoint:
    month: date  # first day of the month
    income: Decimal
    expense: Decimal

    @property
    def balance(self) -> Decimal:
        return self.income - self.expense

    def value(self, metric: str) -> Decimal:
        return {EXPENSE: self.expense, INCOME: self.income}.get(metric, self.balance)


@dataclass(frozen=True)
class MonthlyTrend:
    points: tuple[MonthlyPoint, ...]
    metric: str  # EXPENSE | INCOME | BALANCE

    @property
    def average(self) -> Decimal:
        """Average over ALL months of the window; months without movement count as zero."""
        if not self.points:
            return ZERO
        total = sum((p.value(self.metric) for p in self.points), ZERO)
        return (total / len(self.points)).quantize(TWO_PLACES, ROUND_HALF_UP)

    @property
    def has_data(self) -> bool:
        return any(p.income or p.expense for p in self.points)


@dataclass(frozen=True)
class CategoryChange:
    category_id: int
    category_name: str
    current: Decimal
    previous: Decimal

    @property
    def change(self) -> Decimal:
        return self.current - self.previous

    @property
    def percent(self) -> Decimal | None:
        """None when the previous period had nothing (no division by zero)."""
        return percent(self.change, self.previous)


# --- PERIOD_SUMMARY ----------------------------------------------------------------------------


def period_summary(
    *, scope: ReadScope, start_date: date, end_date: date, top_n: int = DEFAULT_TOP_CATEGORIES
) -> PeriodSummary:
    _check(scope, start_date, end_date)
    qs = scope.transactions().filter(transaction_date__range=(start_date, end_date))
    totals = qs.aggregate(
        income_total=_sum(Q(type=INCOME)),
        expense_total=_sum(Q(type=EXPENSE)),
        income_count=Count("id", filter=Q(type=INCOME)),
        expense_count=Count("id", filter=Q(type=EXPENSE)),
    )
    common = {"scope": scope, "start_date": start_date, "end_date": end_date, "limit": 1}
    largest_expense = _highlight(get_top_expenses(type=EXPENSE, **common))
    largest_income = _highlight(get_top_expenses(type=INCOME, **common))
    categories = get_totals_by_category(
        scope=scope, start_date=start_date, end_date=end_date, type=EXPENSE
    )[:top_n]
    return PeriodSummary(
        income_total=totals["income_total"],
        expense_total=totals["expense_total"],
        income_count=totals["income_count"],
        expense_count=totals["expense_count"],
        largest_expense=largest_expense,
        largest_income=largest_income,
        top_categories=tuple(categories),
    )


# --- MONTHLY_TREND -----------------------------------------------------------------------------


def month_window(months: int, end_month: date) -> list[date]:
    """First days of the ``months`` calendar months ending at ``end_month`` (inclusive)."""
    if not 1 <= months <= MAX_MONTHS:
        raise ValueError(f"months deve estar entre 1 e {MAX_MONTHS}.")
    first = end_month.replace(day=1)
    window = [first]
    for _ in range(months - 1):
        first = (first - timedelta(days=1)).replace(day=1)
        window.append(first)
    return list(reversed(window))


def monthly_trend(
    *, scope: ReadScope, months: int, end_month: date, metric: str = EXPENSE
) -> MonthlyTrend:
    """One GROUP BY month query; months without movement are filled with zero."""
    window = month_window(months, end_month)
    last_day = (window[-1].replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    rows = (
        scope.transactions()
        .filter(transaction_date__range=(window[0], last_day))
        .annotate(month=TruncMonth("transaction_date"))
        .values("month")
        .annotate(income=_sum(Q(type=INCOME)), expense=_sum(Q(type=EXPENSE)))
        .order_by("month")
    )
    by_month = {row["month"]: row for row in rows}
    points = tuple(
        MonthlyPoint(
            month=month,
            income=by_month.get(month, {}).get("income", ZERO),
            expense=by_month.get(month, {}).get("expense", ZERO),
        )
        for month in window
    )
    return MonthlyTrend(points=points, metric=metric if metric in (EXPENSE, INCOME) else "BALANCE")


# --- CATEGORY_TREND ----------------------------------------------------------------------------


INCREASE, DECREASE, ALL = "INCREASE", "DECREASE", "ALL"


def category_trend(
    *,
    scope: ReadScope,
    type: str,
    start_date: date,
    end_date: date,
    compare_start_date: date,
    compare_end_date: date,
    direction: str = ALL,
    category_id: int | None = None,
    limit: int = 5,
) -> list[CategoryChange]:
    """Per-category totals for period A (current) and B (previous) in ONE query.

    INCREASE: categories that grew (largest first); DECREASE: that shrank (largest drop
    first); ALL: any change, largest absolute change first. Unchanged categories only in ALL.
    """
    _check(scope, start_date, end_date)
    _check(scope, compare_start_date, compare_end_date)
    in_a = Q(transaction_date__range=(start_date, end_date))
    in_b = Q(transaction_date__range=(compare_start_date, compare_end_date))
    qs = scope.transactions().filter(in_a | in_b, type=type)
    if category_id is not None:
        qs = qs.filter(category_id=category_id)
    rows = (
        qs.values("category_id", "category__name")
        .annotate(current=_sum(in_a), previous=_sum(in_b))
        .order_by("category__name")
    )
    changes = [
        CategoryChange(
            category_id=row["category_id"],
            category_name=row["category__name"],
            current=row["current"],
            previous=row["previous"],
        )
        for row in rows
    ]
    if direction == INCREASE:
        changes = sorted((c for c in changes if c.change > 0), key=lambda c: -c.change)
    elif direction == DECREASE:
        changes = sorted((c for c in changes if c.change < 0), key=lambda c: c.change)
    else:
        changes = sorted(changes, key=lambda c: (-abs(c.change), c.category_name))
    return changes[:limit]


# --- helpers ------------------------------------------------------------------------------------


def _sum(condition: Q):
    return Coalesce(Sum("amount", filter=condition), Value(ZERO), output_field=_MONEY)


def _highlight(queryset) -> TransactionHighlight | None:
    txn: Transaction | None = next(iter(queryset), None)
    if txn is None:
        return None
    if txn.description:
        label = txn.description[:1].upper() + txn.description[1:]
    else:
        label = txn.subcategory.name if txn.subcategory else txn.category.name
    return TransactionHighlight(label=label, amount=txn.amount)


def _check(scope: ReadScope, start: date, end: date) -> None:
    if not isinstance(scope, ReadScope):
        raise TypeError("scope must be a ReadScope from apps.transactions.scopes.")
    if start > end:
        raise ValueError("Data inicial deve ser anterior ou igual à data final.")
