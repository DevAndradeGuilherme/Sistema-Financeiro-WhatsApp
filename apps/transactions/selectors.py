"""Deterministic read queries. All aggregation happens in PostgreSQL.

Every selector requires *whose* data is read, as a keyword argument: either ``user`` (the
authenticated actor's own data) or ``scope`` (a ``ReadScope`` built and authorized by
``apps.transactions.scopes``). There is intentionally no selector over arbitrary users'
transactions. Soft-deleted transactions are always excluded.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import DecimalField, Q, QuerySet, Sum, Value
from django.db.models.functions import Coalesce

from apps.accounts.models import User
from apps.categories.models import Category, Subcategory
from common.money import percent

from .models import Transaction, TransactionType
from .scopes import FAMILY, ReadScope, own

ZERO = Decimal("0.00")
MAX_TOP_LIMIT = 50
_MONEY = DecimalField(max_digits=14, decimal_places=2)


@dataclass(frozen=True)
class PeriodTotals:
    income: Decimal
    expense: Decimal

    @property
    def balance(self) -> Decimal:
        return self.income - self.expense


@dataclass(frozen=True)
class CategoryTotal:
    category_id: int
    category_name: str
    total: Decimal


@dataclass(frozen=True)
class MemberTotal:
    user_id: int
    name: str
    total: Decimal


def list_transactions(
    *,
    user: User | None = None,
    scope: ReadScope | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    type: str | None = None,
    category: Category | None = None,
    subcategory: Subcategory | None = None,
) -> QuerySet[Transaction]:
    """Active transactions in scope, newest first, optionally filtered."""
    qs = _base_queryset(user, scope)
    if start_date is not None or end_date is not None:
        _validate_period(start_date, end_date, required=False)
    if start_date is not None:
        qs = qs.filter(transaction_date__gte=start_date)
    if end_date is not None:
        qs = qs.filter(transaction_date__lte=end_date)
    if type is not None:
        _validate_type(type)
        qs = qs.filter(type=type)
    if category is not None:
        qs = qs.filter(category=category)
    if subcategory is not None:
        qs = qs.filter(subcategory=subcategory)
    return qs.select_related("category", "subcategory", "user").order_by(
        "-transaction_date", "-created_at"
    )


def get_total(
    *,
    user: User | None = None,
    scope: ReadScope | None = None,
    start_date: date,
    end_date: date,
    type: str,
    category: Category | None = None,
    subcategory: Subcategory | None = None,
) -> Decimal:
    """Sum of ``type`` transactions in the inclusive period, optionally by category/subcategory."""
    _validate_period(start_date, end_date)
    _validate_type(type)
    qs = _period_queryset(user, scope, start_date, end_date).filter(type=type)
    if category is not None:
        qs = qs.filter(category=category)
    if subcategory is not None:
        qs = qs.filter(subcategory=subcategory)
    return qs.aggregate(total=Coalesce(Sum("amount"), Value(ZERO), output_field=_MONEY))["total"]


def get_period_totals(
    *,
    user: User | None = None,
    scope: ReadScope | None = None,
    start_date: date,
    end_date: date,
) -> PeriodTotals:
    """Income and expense totals (and balance) for the inclusive period, in one query."""
    _validate_period(start_date, end_date)
    result = _period_queryset(user, scope, start_date, end_date).aggregate(
        income=Coalesce(
            Sum("amount", filter=Q(type=TransactionType.INCOME)), Value(ZERO), output_field=_MONEY
        ),
        expense=Coalesce(
            Sum("amount", filter=Q(type=TransactionType.EXPENSE)), Value(ZERO), output_field=_MONEY
        ),
    )
    return PeriodTotals(income=result["income"], expense=result["expense"])


def get_totals_by_category(
    *,
    user: User | None = None,
    scope: ReadScope | None = None,
    start_date: date,
    end_date: date,
    type: str,
) -> list[CategoryTotal]:
    """Totals grouped by category, highest first (one GROUP BY query)."""
    _validate_period(start_date, end_date)
    _validate_type(type)
    rows = (
        _period_queryset(user, scope, start_date, end_date)
        .filter(type=type)
        .values("category_id", "category__name")
        .annotate(total=Sum("amount"))
        .order_by("-total", "category__name")
    )
    return [
        CategoryTotal(
            category_id=row["category_id"], category_name=row["category__name"], total=row["total"]
        )
        for row in rows
    ]


def get_member_totals(
    *, scope: ReadScope, start_date: date, end_date: date, type: str
) -> list[MemberTotal]:
    """Totals per family member, highest first (one GROUP BY query). FAMILY scope only."""
    if not isinstance(scope, ReadScope) or scope.kind != FAMILY:
        raise TypeError("get_member_totals requires an authorized FAMILY scope.")
    _validate_period(start_date, end_date)
    _validate_type(type)
    rows = (
        _period_queryset(None, scope, start_date, end_date)
        .filter(type=type)
        .values("user_id", "user__first_name", "user__last_name", "user__username")
        .annotate(total=Sum("amount"))
        .order_by("-total", "user__first_name", "user_id")
    )
    return [
        MemberTotal(
            user_id=row["user_id"],
            name=" ".join(
                part for part in (row["user__first_name"], row["user__last_name"]) if part
            )
            or row["user__username"],
            total=row["total"],
        )
        for row in rows
    ]


def get_top_expenses(
    *,
    user: User | None = None,
    scope: ReadScope | None = None,
    start_date: date,
    end_date: date,
    limit: int = 5,
    type: str = TransactionType.EXPENSE,
) -> QuerySet[Transaction]:
    """Largest expenses (or incomes, with ``type=INCOME``) in the inclusive period."""
    _validate_period(start_date, end_date)
    _validate_type(type)
    if not 1 <= limit <= MAX_TOP_LIMIT:
        raise ValidationError(f"limit deve estar entre 1 e {MAX_TOP_LIMIT}.")
    return (
        _period_queryset(user, scope, start_date, end_date)
        .filter(type=type)
        .select_related("category", "subcategory", "user")
        .order_by("-amount", "-transaction_date", "-created_at")[:limit]
    )


@dataclass(frozen=True)
class PeriodComparison:
    current: Decimal  # period A
    previous: Decimal  # period B

    @property
    def difference(self) -> Decimal:
        return self.current - self.previous

    @property
    def percent(self) -> Decimal | None:
        """(A - B) / B * 100, one decimal place, ROUND_HALF_UP. None when B is zero."""
        return percent(self.difference, self.previous)


def compare_periods(
    *,
    user: User | None = None,
    scope: ReadScope | None = None,
    type: str,
    start_date: date,
    end_date: date,
    compare_start_date: date,
    compare_end_date: date,
    category: Category | None = None,
    subcategory: Subcategory | None = None,
) -> PeriodComparison:
    """Totals of two inclusive periods (A = start/end, B = compare_*), computed in SQL."""
    common = {
        "scope": scope or own(_require_user(user)),
        "type": type,
        "category": category,
        "subcategory": subcategory,
    }
    return PeriodComparison(
        current=get_total(start_date=start_date, end_date=end_date, **common),
        previous=get_total(start_date=compare_start_date, end_date=compare_end_date, **common),
    )


def find_transactions(
    *,
    user: User,
    since: date,
    type: str | None = None,
    category: Category | None = None,
    subcategory: Subcategory | None = None,
    description: str | None = None,
    amount: Decimal | None = None,
    transaction_date: date | None = None,
    limit: int = 2,
) -> list[Transaction]:
    """Candidates for a reference like "o gasto de 50 no mercado", newest registered first.

    Only the user's OWN active transactions (used to pick what to edit: never a scope),
    with ``transaction_date >= since``. Callers use ``limit=2`` to detect ambiguity.
    """
    if not 1 <= limit <= MAX_TOP_LIMIT:
        raise ValidationError(f"limit deve estar entre 1 e {MAX_TOP_LIMIT}.")
    qs = _base_queryset(user, None).filter(transaction_date__gte=since)
    if type is not None:
        _validate_type(type)
        qs = qs.filter(type=type)
    if category is not None:
        qs = qs.filter(category=category)
    if subcategory is not None:
        qs = qs.filter(subcategory=subcategory)
    if description:
        qs = qs.filter(description__icontains=description.strip())
    if amount is not None:
        qs = qs.filter(amount=amount)
    if transaction_date is not None:
        qs = qs.filter(transaction_date=transaction_date)
    return list(qs.select_related("category", "subcategory").order_by("-created_at", "-id")[:limit])


def _require_user(user: User | None) -> User:
    if not isinstance(user, User) or user.pk is None:
        raise TypeError("user must be a saved User instance.")
    return user


def _base_queryset(user: User | None, scope: ReadScope | None) -> QuerySet[Transaction]:
    if scope is not None:
        if user is not None:
            raise TypeError("Pass either user or scope, not both.")
        if not isinstance(scope, ReadScope):
            raise TypeError("scope must be a ReadScope from apps.transactions.scopes.")
        return scope.transactions()
    return own(_require_user(user)).transactions()


def _period_queryset(
    user: User | None, scope: ReadScope | None, start_date: date, end_date: date
) -> QuerySet[Transaction]:
    return _base_queryset(user, scope).filter(transaction_date__range=(start_date, end_date))


def _validate_period(start_date: date | None, end_date: date | None, required: bool = True) -> None:
    if required and (start_date is None or end_date is None):
        raise ValidationError("Período deve ter data inicial e final.")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValidationError("Data inicial deve ser anterior ou igual à data final.")


def _validate_type(type: str) -> None:
    if type not in TransactionType.values:
        raise ValidationError("Tipo de transação inválido.")
