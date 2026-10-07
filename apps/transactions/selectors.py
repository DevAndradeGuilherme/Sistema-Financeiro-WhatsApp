"""Deterministic read queries. All aggregation happens in PostgreSQL.

Every selector requires the ``user`` whose data is read, as a keyword argument. Callers pass
the authenticated actor; there is intentionally no selector over all users' transactions.
Soft-deleted transactions are always excluded.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import DecimalField, Q, QuerySet, Sum, Value
from django.db.models.functions import Coalesce

from apps.accounts.models import User
from apps.categories.models import Category, Subcategory

from .models import Transaction, TransactionType

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


def list_transactions(
    *,
    user: User,
    start_date: date | None = None,
    end_date: date | None = None,
    type: str | None = None,
    category: Category | None = None,
    subcategory: Subcategory | None = None,
) -> QuerySet[Transaction]:
    """Active transactions of ``user``, newest first, optionally filtered."""
    qs = _base_queryset(user)
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
    return qs.select_related("category", "subcategory").order_by("-transaction_date", "-created_at")


def get_total(
    *,
    user: User,
    start_date: date,
    end_date: date,
    type: str,
    category: Category | None = None,
    subcategory: Subcategory | None = None,
) -> Decimal:
    """Sum of ``type`` transactions in the inclusive period, optionally by category/subcategory."""
    _validate_period(start_date, end_date)
    _validate_type(type)
    qs = _period_queryset(user, start_date, end_date).filter(type=type)
    if category is not None:
        qs = qs.filter(category=category)
    if subcategory is not None:
        qs = qs.filter(subcategory=subcategory)
    return qs.aggregate(total=Coalesce(Sum("amount"), Value(ZERO), output_field=_MONEY))["total"]


def get_period_totals(*, user: User, start_date: date, end_date: date) -> PeriodTotals:
    """Income and expense totals (and balance) for the inclusive period, in one query."""
    _validate_period(start_date, end_date)
    result = _period_queryset(user, start_date, end_date).aggregate(
        income=Coalesce(
            Sum("amount", filter=Q(type=TransactionType.INCOME)), Value(ZERO), output_field=_MONEY
        ),
        expense=Coalesce(
            Sum("amount", filter=Q(type=TransactionType.EXPENSE)), Value(ZERO), output_field=_MONEY
        ),
    )
    return PeriodTotals(income=result["income"], expense=result["expense"])


def get_totals_by_category(
    *, user: User, start_date: date, end_date: date, type: str
) -> list[CategoryTotal]:
    """Totals grouped by category, highest first."""
    _validate_period(start_date, end_date)
    _validate_type(type)
    rows = (
        _period_queryset(user, start_date, end_date)
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


def get_top_expenses(
    *, user: User, start_date: date, end_date: date, limit: int = 5
) -> QuerySet[Transaction]:
    """Largest expenses in the inclusive period."""
    _validate_period(start_date, end_date)
    if not 1 <= limit <= MAX_TOP_LIMIT:
        raise ValidationError(f"limit deve estar entre 1 e {MAX_TOP_LIMIT}.")
    return (
        _period_queryset(user, start_date, end_date)
        .filter(type=TransactionType.EXPENSE)
        .select_related("category", "subcategory")
        .order_by("-amount", "-transaction_date", "-created_at")[:limit]
    )


def _base_queryset(user: User) -> QuerySet[Transaction]:
    if not isinstance(user, User) or user.pk is None:
        raise TypeError("user must be a saved User instance.")
    return Transaction.objects.active().filter(user=user)


def _period_queryset(user: User, start_date: date, end_date: date) -> QuerySet[Transaction]:
    return _base_queryset(user).filter(transaction_date__range=(start_date, end_date))


def _validate_period(start_date: date | None, end_date: date | None, required: bool = True) -> None:
    if required and (start_date is None or end_date is None):
        raise ValidationError("Período deve ter data inicial e final.")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValidationError("Data inicial deve ser anterior ou igual à data final.")


def _validate_type(type: str) -> None:
    if type not in TransactionType.values:
        raise ValidationError("Tipo de transação inválido.")
