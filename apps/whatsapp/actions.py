"""Turn an untrusted ``Interpretation`` into a validated plan and execute it.

Django is the authority here:
- ids proposed by the model are re-checked against the catalog (active, compatible type);
- amounts/dates are parsed locally (Decimal, ISO dates) and re-validated by the domain service;
- the user is always the authenticated sender passed by the caller — never a model field;
- the transaction to edit is chosen by Django in the sender's own data (never by an id
  from the model); ambiguous references change nothing;
- totals, rankings, groupings and percentages come only from
  ``apps.transactions.selectors`` (PostgreSQL / Decimal).

``plan`` only reads; ``execute`` performs the effect and must run inside the caller's
atomic block (see ``pipeline.handle_ready_message``).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.ai.contract import (
    CatalogCategory,
    CatalogSubcategory,
    Interpretation,
    InterpretationContext,
    TransactionReference,
)
from apps.categories.models import Category, Subcategory
from apps.categories.selectors import get_fallback_category, list_active_categories
from apps.transactions import selectors
from apps.transactions.models import (
    Transaction,
    TransactionChange,
    TransactionSource,
    TransactionType,
)
from apps.transactions.services import (
    UNSET,
    create_transaction,
    undo_last_change,
    update_transaction,
)
from common.money import InvalidAmount, to_decimal

from . import messages

MAX_DESCRIPTION = 255
DEFAULT_LIMIT = 5
MAX_LIMIT = 10
REFERENCE_WINDOW_DAYS = 90  # how far back "o gasto de gasolina" is searched


def build_context(
    *, today: date, timezone: str, previous_query: dict | None = None
) -> InterpretationContext:
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
    return InterpretationContext(
        reference_date=today, timezone=timezone, catalog=catalog, previous_query=previous_query
    )


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
    kind: str  # TOTAL | BALANCE | TOP | BREAKDOWN | LIST | COMPARE
    type: str | None
    category: Category | None
    subcategory: Subcategory | None
    start: date | None
    end: date | None
    compare_start: date | None = None
    compare_end: date | None = None
    limit: int | None = None

    def as_context(self) -> dict:
        """Validated parameters only (ids, dates, kinds) for follow-ups."""
        return {
            "query_kind": self.kind,
            "transaction_type": self.type,
            "category_id": self.category.pk if self.category else None,
            "subcategory_id": self.subcategory.pk if self.subcategory else None,
            "start_date": self.start.isoformat() if self.start else None,
            "end_date": self.end.isoformat() if self.end else None,
            "compare_start_date": self.compare_start.isoformat() if self.compare_start else None,
            "compare_end_date": self.compare_end.isoformat() if self.compare_end else None,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class UpdatePlan:
    intent: str
    transaction_id: object
    changes: dict = field(default_factory=dict)  # only fields to change (service kwargs)


@dataclass(frozen=True)
class UndoPlan:
    intent: str


Plan = ReplyPlan | CreatePlan | QueryPlan | UpdatePlan | UndoPlan


@dataclass(frozen=True)
class Outcome:
    intent: str
    reply: str
    code: str = ""  # non-empty: answered with a rejection / no effect
    transaction: Transaction | None = None
    query_context: dict | None = None  # to remember for follow-ups


def plan(
    interpretation: Interpretation, *, user: User, today: date, has_context: bool = False
) -> Plan:
    i = interpretation
    if i.follow_up and not has_context:
        return ReplyPlan(i.intent, messages.NO_CONTEXT, "NO_CONTEXT")
    if i.intent == "HELP":
        return ReplyPlan(i.intent, messages.HELP)
    if i.intent == "CREATE_TRANSACTION":
        return _plan_create(i)
    if i.intent == "QUERY_TRANSACTIONS":
        return _plan_query(i, today)
    if i.intent == "UPDATE_TRANSACTION":
        return _plan_update(i, user, today)
    if i.intent == "UNDO_TRANSACTION":
        return UndoPlan(i.intent)
    return ReplyPlan(i.intent, messages.NOT_UNDERSTOOD, "NOT_UNDERSTOOD")


# --- CREATE ------------------------------------------------------------------------------


def _plan_create(i: Interpretation) -> Plan:
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


# --- QUERY -------------------------------------------------------------------------------


def _plan_query(i: Interpretation, today: date) -> Plan:
    kind = i.query_kind
    transaction_type = i.transaction_type
    if kind == "TOTAL" and transaction_type not in TransactionType.values:
        return ReplyPlan(i.intent, messages.NOT_UNDERSTOOD, "NOT_UNDERSTOOD")
    if kind == "BALANCE":
        transaction_type = None
    elif kind == "TOP":
        transaction_type = TransactionType.EXPENSE  # "maiores gastos"
    elif kind in ("BREAKDOWN", "COMPARE"):
        transaction_type = transaction_type or TransactionType.EXPENSE
    elif kind not in ("TOTAL", "LIST"):
        return ReplyPlan(i.intent, messages.NOT_UNDERSTOOD, "NOT_UNDERSTOOD")

    period = _period(i.start_date, i.end_date, today, default_month=kind != "LIST")
    if isinstance(period, ReplyPlan):
        return ReplyPlan(i.intent, period.reply, period.code)
    start, end = period

    compare_start = compare_end = None
    if kind == "COMPARE":
        if i.compare_start_date is None and i.compare_end_date is None:
            compare_end = start.replace(day=1) - timedelta(days=1)  # previous full month
            compare_start = compare_end.replace(day=1)
        else:
            b = _period(i.compare_start_date, i.compare_end_date, today, default_month=True)
            if isinstance(b, ReplyPlan):
                return ReplyPlan(i.intent, b.reply, b.code)
            compare_start, compare_end = b

    category = subcategory = None
    if kind in ("TOTAL", "LIST", "COMPARE"):
        resolved = _resolve_for_query(i.category_id, i.subcategory_id, transaction_type)
        if resolved is None:
            return ReplyPlan(i.intent, messages.CATEGORY_NOT_FOUND, "CATEGORY_NOT_FOUND")
        category, subcategory = resolved

    limit = None
    if kind in ("TOP", "LIST"):
        limit = DEFAULT_LIMIT if i.limit is None or i.limit < 1 else min(i.limit, MAX_LIMIT)

    return QueryPlan(
        i.intent,
        kind,
        transaction_type,
        category,
        subcategory,
        start,
        end,
        compare_start,
        compare_end,
        limit,
    )


def _period(start_raw, end_raw, today: date, *, default_month: bool):
    """(start, end) validated; missing → current month to date (or no period for lists)."""
    start = _parse_date(start_raw) if start_raw else None
    end = _parse_date(end_raw) if end_raw else None
    if (start_raw and start is None) or (end_raw and end is None):
        return ReplyPlan("", messages.INVALID_DATE, "INVALID_DATE")
    if start is None and end is None:
        if not default_month:
            return None, None
        start, end = today.replace(day=1), today
    elif end is None:
        end = max(start, today)
    elif start is None:
        start = end
    if start > end:
        return ReplyPlan("", messages.INVALID_PERIOD, "INVALID_PERIOD")
    return start, end


def _resolve_for_query(category_id, subcategory_id, transaction_type):
    """(category, subcategory) for filters, or None when the proposal is invalid."""
    category = subcategory = None
    if category_id is not None:
        category = Category.objects.filter(pk=category_id).first()
        if category is None or (transaction_type and not category.accepts(transaction_type)):
            return None
    if subcategory_id is not None:
        subcategory = (
            Subcategory.objects.select_related("category").filter(pk=subcategory_id).first()
        )
        if subcategory is None:
            return None
        if transaction_type and not subcategory.category.accepts(transaction_type):
            return None
        if category is not None and subcategory.category_id != category.pk:
            return None
    return category, subcategory


# --- UPDATE ------------------------------------------------------------------------------


def _plan_update(i: Interpretation, user: User, today: date) -> Plan:
    updates = i.updates
    if updates is None or updates.is_empty():
        return ReplyPlan(i.intent, messages.NOTHING_TO_UPDATE, "NOTHING_TO_UPDATE")

    changes: dict = {}
    if updates.amount is not None:
        try:
            changes["amount"] = to_decimal(updates.amount)
        except InvalidAmount:
            return ReplyPlan(i.intent, messages.INVALID_AMOUNT, "INVALID_AMOUNT")
    if updates.transaction_date is not None:
        changes["transaction_date"] = _parse_date(updates.transaction_date)
        if changes["transaction_date"] is None:
            return ReplyPlan(i.intent, messages.INVALID_DATE, "INVALID_DATE")
    if updates.description is not None:
        changes["description"] = updates.description[:MAX_DESCRIPTION]
    if updates.subcategory_id is not None:
        sub = (
            Subcategory.objects.select_related("category").filter(pk=updates.subcategory_id).first()
        )
        if sub is None:
            return ReplyPlan(i.intent, messages.CATEGORY_NOT_FOUND, "CATEGORY_NOT_FOUND")
        changes["subcategory"] = sub
        changes["category"] = sub.category
    if updates.category_id is not None:
        category = Category.objects.filter(pk=updates.category_id).first()
        if category is None:
            return ReplyPlan(i.intent, messages.CATEGORY_NOT_FOUND, "CATEGORY_NOT_FOUND")
        if "subcategory" in changes and changes["category"].pk != category.pk:
            changes.pop("subcategory")  # inconsistent proposal: keep the explicit category
        changes["category"] = category

    target = _resolve_reference(i.reference, user, today)
    if isinstance(target, ReplyPlan):
        return ReplyPlan(i.intent, target.reply, target.code)
    return UpdatePlan(i.intent, target.pk, changes)


def _resolve_reference(
    reference: TransactionReference | None, user: User, today: date
) -> Transaction | ReplyPlan:
    """Pick the sender's transaction described by the reference. Never guesses."""
    reference = reference or TransactionReference(last=True)
    criteria: dict = {}
    if reference.transaction_type in TransactionType.values:
        criteria["type"] = reference.transaction_type
    if reference.category_id is not None:
        criteria["category"] = Category.objects.filter(pk=reference.category_id).first()
    if reference.subcategory_id is not None:
        criteria["subcategory"] = Subcategory.objects.filter(pk=reference.subcategory_id).first()
    if reference.description and not ({"category", "subcategory"} & criteria.keys()):
        # The model often repeats the item ("gasolina") as description; when a catalog
        # criterion exists it already identifies the item, and free text would be too strict.
        criteria["description"] = reference.description[:MAX_DESCRIPTION]
    if reference.amount is not None:
        try:
            criteria["amount"] = to_decimal(reference.amount)
        except InvalidAmount:
            criteria["amount"] = None
    if reference.transaction_date is not None:
        criteria["transaction_date"] = _parse_date(reference.transaction_date)

    # A criterion the model sent but that does not resolve can only mean "not found".
    if any(value is None for value in criteria.values()):
        return ReplyPlan("", messages.NOT_FOUND, "NOT_FOUND")

    pick_latest = reference.last or not (criteria.keys() - {"type"})
    candidates = selectors.find_transactions(
        user=user,
        since=today - timedelta(days=REFERENCE_WINDOW_DAYS),
        limit=1 if pick_latest else 2,
        **criteria,
    )
    if not candidates:
        return ReplyPlan("", messages.NOT_FOUND, "NOT_FOUND")
    if len(candidates) > 1:
        is_income = {"INCOME": True, "EXPENSE": False}.get(criteria.get("type"))
        return ReplyPlan(
            "",
            messages.ambiguous(is_income=is_income, description=reference.description),
            "AMBIGUOUS_REFERENCE",
        )
    return candidates[0]


def _parse_date(value: str | None) -> date | None:
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
    if isinstance(plan_, UpdatePlan):
        return _execute_update(plan_, user, idempotency_key)
    if isinstance(plan_, UndoPlan):
        return _execute_undo(plan_, user, idempotency_key)
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
        return _rejected(p.intent, exc)

    reply = messages.transaction_created(
        is_income=txn.type == TransactionType.INCOME,
        amount=txn.amount,
        category=txn.category.name,
        subcategory=txn.subcategory.name if txn.subcategory else None,
        transaction_date=txn.transaction_date,
        today=today,
    )
    return Outcome(p.intent, reply, transaction=txn)


def _execute_update(p: UpdatePlan, user: User, idempotency_key: str) -> Outcome:
    kwargs = {name: p.changes.get(name, UNSET) for name in _UPDATABLE}
    try:
        result = update_transaction(
            user=user, transaction_id=p.transaction_id, origin=idempotency_key, **kwargs
        )
    except PermissionDenied:
        return Outcome(p.intent, messages.NOT_FOUND, "NOT_FOUND")
    except ValidationError as exc:
        return _rejected(p.intent, exc)

    txn = result.transaction
    if result.change is None:
        return Outcome(p.intent, messages.NO_CHANGE, "NO_CHANGE", transaction=txn)
    before, after = result.change.before, result.change.after
    reply = messages.transaction_updated(
        is_income=txn.type == TransactionType.INCOME,
        old_amount=Decimal(before["amount"]),
        new_amount=txn.amount,
        old_where=_where_from_snapshot(before),
        new_where=_where(txn),
        new_date=txn.transaction_date
        if before["transaction_date"] != after["transaction_date"]
        else None,
        new_description=txn.description if before["description"] != after["description"] else None,
    )
    return Outcome(p.intent, reply, transaction=txn)


_UPDATABLE = ("amount", "description", "category", "subcategory", "transaction_date")


def _execute_undo(p: UndoPlan, user: User, idempotency_key: str) -> Outcome:
    result = undo_last_change(user=user, origin=idempotency_key)
    if result is None:
        return Outcome(p.intent, messages.NOTHING_TO_UNDO, "NOTHING_TO_UNDO")
    txn = result.transaction
    if result.reverted.action == TransactionChange.Action.CREATE:
        reply = messages.undo_create(amount=txn.amount, where=_where(txn))
    else:
        reply = messages.undo_update(
            is_income=txn.type == TransactionType.INCOME, amount=txn.amount, where=_where(txn)
        )
    return Outcome(p.intent, reply, transaction=txn)


def _execute_query(p: QueryPlan, user: User, today: date) -> Outcome:
    is_income = p.type == TransactionType.INCOME
    context = p.as_context()

    if p.kind == "BALANCE":
        totals = selectors.get_period_totals(user=user, start_date=p.start, end_date=p.end)
        reply = messages.balance(
            income=totals.income, expense=totals.expense, start=p.start, end=p.end, today=today
        )
    elif p.kind == "TOP":
        rows = selectors.get_top_expenses(
            user=user, start_date=p.start, end_date=p.end, limit=p.limit
        )
        reply = messages.top_expenses(
            [(_item_label(t), t.amount) for t in rows], start=p.start, end=p.end, today=today
        )
    elif p.kind == "BREAKDOWN":
        rows = selectors.get_totals_by_category(
            user=user, start_date=p.start, end_date=p.end, type=p.type
        )
        reply = messages.breakdown(
            [(r.category_name, r.total) for r in rows],
            is_income=is_income,
            start=p.start,
            end=p.end,
            today=today,
        )
    elif p.kind == "LIST":
        rows = selectors.list_transactions(
            user=user,
            start_date=p.start,
            end_date=p.end,
            type=p.type,
            category=p.category,
            subcategory=p.subcategory,
        )[: p.limit]
        reply = messages.listing(
            [
                (t.transaction_date, _item_label(t), t.amount, t.type == TransactionType.INCOME)
                for t in rows
            ],
            type_=p.type,
            period=(p.start, p.end) if p.start else None,
            today=today,
        )
    elif p.kind == "COMPARE":
        result = selectors.compare_periods(
            user=user,
            type=p.type,
            start_date=p.start,
            end_date=p.end,
            compare_start_date=p.compare_start,
            compare_end_date=p.compare_end,
            category=p.category,
            subcategory=p.subcategory,
        )
        reply = messages.comparison(
            is_income=is_income,
            current=result.current,
            previous=result.previous,
            percent=result.percent,
            scope=_scope(p),
            period_a=(p.start, p.end),
            period_b=(p.compare_start, p.compare_end),
            today=today,
        )
    else:  # TOTAL
        value = selectors.get_total(
            user=user,
            start_date=p.start,
            end_date=p.end,
            type=p.type,
            category=p.category,
            subcategory=p.subcategory,
        )
        reply = messages.total(
            is_income=is_income, value=value, scope=_scope(p), start=p.start, end=p.end, today=today
        )
    return Outcome(p.intent, reply, query_context=context)


# --- Helpers -------------------------------------------------------------------------------


def _scope(p: QueryPlan) -> str | None:
    return p.subcategory.name if p.subcategory else (p.category.name if p.category else None)


def _where(txn: Transaction) -> str:
    return messages.where(txn.category.name, txn.subcategory.name if txn.subcategory else None)


def _where_from_snapshot(data: dict) -> str:
    category = Category.objects.filter(pk=data["category_id"]).first()
    sub = Subcategory.objects.filter(pk=data["subcategory_id"]).first()
    return messages.where(category.name if category else "?", sub.name if sub else None)


def _item_label(txn: Transaction) -> str:
    if txn.description:
        return txn.description[:1].upper() + txn.description[1:]
    return txn.subcategory.name if txn.subcategory else txn.category.name


def _rejected(intent: str, exc: ValidationError) -> Outcome:
    field_name, reason = _first_error(exc)
    return Outcome(intent, messages.rejected(reason), f"REJECTED_{field_name.upper()}"[:50])


def _first_error(exc: ValidationError) -> tuple[str, str]:
    if hasattr(exc, "message_dict"):
        for field_name, errors in exc.message_dict.items():
            if errors:
                return field_name, errors[0]
    return "invalid", exc.messages[0] if exc.messages else "dados inválidos."
