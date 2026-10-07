"""Turn an untrusted ``Interpretation`` into a validated plan and execute it.

Django is the authority here:
- ids proposed by the model are re-checked against the catalog (active, compatible type);
- amounts/dates are parsed locally (Decimal, ISO dates) and re-validated by the domain service;
- the user is always the authenticated sender passed by the caller — never a model field;
- totals come only from ``apps.transactions.selectors`` (PostgreSQL).

``plan`` only reads; ``execute`` performs the effect and must run inside the caller's
atomic block (see ``pipeline.handle_ready_message``).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured, ValidationError

from apps.accounts.models import User
from apps.ai.contract import (
    CatalogCategory,
    CatalogSubcategory,
    Interpretation,
    InterpretationContext,
)
from apps.categories.models import Category, Subcategory
from apps.categories.selectors import get_fallback_category, list_active_categories
from apps.transactions import selectors
from apps.transactions.models import Transaction, TransactionSource, TransactionType
from apps.transactions.services import create_transaction
from common.money import InvalidAmount, to_decimal

from . import messages

MAX_DESCRIPTION = 255


def build_context(*, today: date, timezone: str) -> InterpretationContext:
    """Active catalog only, with short integer ids. No user data."""
    catalog = tuple(
        CatalogCategory(
            id=category.pk,
            name=category.name,
            kind=category.kind,
            aliases=tuple(category.aliases),
            subcategories=tuple(
                CatalogSubcategory(id=sub.pk, name=sub.name, aliases=tuple(sub.aliases))
                for sub in category.active_subcategories
            ),
        )
        for category in list_active_categories()
    )
    return InterpretationContext(reference_date=today, timezone=timezone, catalog=catalog)


# --- Plans ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplyPlan:
    """Answer without financial effect (help, not understood, invalid input)."""

    intent: str
    reply: str
    code: str = ""


@dataclass(frozen=True)
class CreatePlan:
    intent: str
    type: str
    amount: Decimal
    category: Category
    subcategory: Subcategory | None
    description: str
    transaction_date: date | None


@dataclass(frozen=True)
class QueryPlan:
    intent: str
    kind: str  # TOTAL | BALANCE
    type: str | None
    category: Category | None
    subcategory: Subcategory | None
    start: date
    end: date


Plan = ReplyPlan | CreatePlan | QueryPlan


@dataclass(frozen=True)
class Outcome:
    intent: str
    reply: str
    code: str = ""  # non-empty: answered with a rejection / no effect
    transaction: Transaction | None = None


def plan(interpretation: Interpretation, *, today: date) -> Plan:
    intent = interpretation.intent
    if intent == "HELP":
        return ReplyPlan(intent, messages.HELP)
    if intent == "CREATE_TRANSACTION":
        return _plan_create(interpretation, today)
    if intent == "QUERY_TRANSACTIONS":
        return _plan_query(interpretation, today)
    return ReplyPlan(intent, messages.NOT_UNDERSTOOD, "NOT_UNDERSTOOD")


def _plan_create(i: Interpretation, today: date) -> Plan:
    if i.transaction_type not in TransactionType.values:
        return ReplyPlan(i.intent, messages.NOT_UNDERSTOOD, "NOT_UNDERSTOOD")
    if i.amount is None:
        return ReplyPlan(i.intent, messages.MISSING_AMOUNT, "MISSING_AMOUNT")
    try:
        amount = to_decimal(i.amount)
    except InvalidAmount:
        return ReplyPlan(i.intent, messages.INVALID_AMOUNT, "INVALID_AMOUNT")

    transaction_date = None
    if i.transaction_date is not None:
        transaction_date = _parse_date(i.transaction_date)
        if transaction_date is None:
            return ReplyPlan(i.intent, messages.INVALID_DATE, "INVALID_DATE")

    category, subcategory = _resolve_for_create(i.transaction_type, i.category_id, i.subcategory_id)
    return CreatePlan(
        intent=i.intent,
        type=i.transaction_type,
        amount=amount,
        category=category,
        subcategory=subcategory,
        description=(i.description or "")[:MAX_DESCRIPTION],
        transaction_date=transaction_date,
    )


def _resolve_for_create(
    transaction_type: str, category_id: int | None, subcategory_id: int | None
) -> tuple[Category, Subcategory | None]:
    """Validated catalog entries, falling back to the explicit fallback category."""
    category = None
    if category_id is not None:
        category = Category.objects.filter(pk=category_id, is_active=True).first()
        if category is not None and not category.accepts(transaction_type):
            category = None

    subcategory = None
    if subcategory_id is not None:
        subcategory = (
            Subcategory.objects.select_related("category")
            .filter(pk=subcategory_id, is_active=True, category__is_active=True)
            .first()
        )
        if subcategory is not None and not subcategory.category.accepts(transaction_type):
            subcategory = None

    if category is None and subcategory is not None:
        category = subcategory.category
    if subcategory is not None and subcategory.category_id != category.pk:
        subcategory = None  # inconsistent proposal: keep only the validated category

    if category is None:
        category = get_fallback_category(transaction_type)
        if category is None:
            raise ImproperlyConfigured(f"No active fallback category for {transaction_type}.")
    return category, subcategory


def _plan_query(i: Interpretation, today: date) -> Plan:
    if i.query_kind == "BALANCE":
        kind, transaction_type = "BALANCE", None
    elif i.query_kind == "TOTAL" and i.transaction_type in TransactionType.values:
        kind, transaction_type = "TOTAL", i.transaction_type
    else:
        return ReplyPlan(i.intent, messages.NOT_UNDERSTOOD, "NOT_UNDERSTOOD")

    start = _parse_date(i.start_date) if i.start_date else None
    end = _parse_date(i.end_date) if i.end_date else None
    if (i.start_date and start is None) or (i.end_date and end is None):
        return ReplyPlan(i.intent, messages.INVALID_DATE, "INVALID_DATE")
    if start is None and end is None:
        start, end = today.replace(day=1), today  # default period: current month to date
    elif end is None:
        end = max(start, today)
    elif start is None:
        start = end
    if start > end:
        return ReplyPlan(i.intent, messages.INVALID_PERIOD, "INVALID_PERIOD")

    category = subcategory = None
    if kind == "TOTAL":
        if i.category_id is not None:
            category = Category.objects.filter(pk=i.category_id).first()
            if category is None or not category.accepts(transaction_type):
                return ReplyPlan(i.intent, messages.CATEGORY_NOT_FOUND, "CATEGORY_NOT_FOUND")
        if i.subcategory_id is not None:
            subcategory = (
                Subcategory.objects.select_related("category").filter(pk=i.subcategory_id).first()
            )
            if subcategory is None or not subcategory.category.accepts(transaction_type):
                return ReplyPlan(i.intent, messages.CATEGORY_NOT_FOUND, "CATEGORY_NOT_FOUND")
            if category is not None and subcategory.category_id != category.pk:
                return ReplyPlan(i.intent, messages.CATEGORY_NOT_FOUND, "CATEGORY_NOT_FOUND")
    return QueryPlan(i.intent, kind, transaction_type, category, subcategory, start, end)


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# --- Execution ----------------------------------------------------------------------


def execute(plan_: Plan, *, user: User, idempotency_key: str, today: date) -> Outcome:
    """Run the effect for ``user`` (the authenticated sender). Call inside transaction.atomic."""
    if isinstance(plan_, ReplyPlan):
        return Outcome(plan_.intent, plan_.reply, plan_.code)
    if isinstance(plan_, CreatePlan):
        return _execute_create(plan_, user, idempotency_key, today)
    return _execute_query(plan_, user, today)


def _execute_create(p: CreatePlan, user: User, idempotency_key: str, today: date) -> Outcome:
    try:
        txn = create_transaction(
            user=user,
            type=p.type,
            amount=p.amount,
            category=p.category,
            subcategory=p.subcategory,
            description=p.description,
            transaction_date=p.transaction_date,
            source=TransactionSource.TEXT,
            idempotency_key=idempotency_key,
        )
    except ValidationError as exc:
        field, reason = _first_error(exc)
        return Outcome(p.intent, messages.rejected(reason), f"REJECTED_{field.upper()}"[:50])

    reply = messages.transaction_created(
        is_income=txn.type == TransactionType.INCOME,
        amount=txn.amount,
        category=txn.category.name,
        subcategory=txn.subcategory.name if txn.subcategory else None,
        transaction_date=txn.transaction_date,
        today=today,
    )
    return Outcome(p.intent, reply, transaction=txn)


def _execute_query(p: QueryPlan, user: User, today: date) -> Outcome:
    if p.kind == "BALANCE":
        totals = selectors.get_period_totals(user=user, start_date=p.start, end_date=p.end)
        reply = messages.balance(
            income=totals.income, expense=totals.expense, start=p.start, end=p.end, today=today
        )
        return Outcome(p.intent, reply)

    value = selectors.get_total(
        user=user,
        start_date=p.start,
        end_date=p.end,
        type=p.type,
        category=p.category,
        subcategory=p.subcategory,
    )
    scope = p.subcategory.name if p.subcategory else (p.category.name if p.category else None)
    reply = messages.total(
        is_income=p.type == TransactionType.INCOME,
        value=value,
        scope=scope,
        start=p.start,
        end=p.end,
        today=today,
    )
    return Outcome(p.intent, reply)


def _first_error(exc: ValidationError) -> tuple[str, str]:
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            if errors:
                return field, errors[0]
    return "invalid", exc.messages[0] if exc.messages else "dados inválidos."
