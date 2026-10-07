"""Turn an untrusted ``Interpretation`` into a validated plan and execute it.

Django is the authority here:
- ids proposed by the model are re-checked against the catalog (active, compatible type);
- amounts/dates are parsed locally (Decimal, ISO dates) and re-validated by the domain service;
- the user is always the authenticated sender passed by the caller — never a model field;
- family reads (V1.2) go through ``apps.transactions.scopes``: the model's ``scope`` /
  ``target_member_name`` are hints; authorization (ADMIN of the *current* family, member
  resolved by name inside that family) happens at execution time; mutations are owner-only;
- the transaction to edit is chosen by Django in the sender's own data (never by an id
  from the model); ambiguous references change nothing;
- totals, rankings, groupings and percentages come only from
  ``apps.transactions.selectors`` (PostgreSQL / Decimal).

``plan`` only reads; ``execute`` performs the effect and must run inside the caller's
atomic block (see ``pipeline.handle_ready_message``).
"""

import logging
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
    ReceiptExtraction,
    TransactionReference,
)
from apps.categories.models import Category, Subcategory
from apps.categories.selectors import get_fallback_category, list_active_categories
from apps.families.selectors import member_display_name
from apps.transactions import analytics, scopes, selectors
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

logger = logging.getLogger(__name__)

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
    kind: str  # TOTAL | BALANCE | TOP | BREAKDOWN | LIST | COMPARE | RANKING | V1.5 analytics
    type: str | None
    category: Category | None
    subcategory: Subcategory | None
    start: date | None
    end: date | None
    compare_start: date | None = None
    compare_end: date | None = None
    limit: int | None = None
    # Untrusted hints about whose data; authorized at execution (apps.transactions.scopes).
    scope_kind: str = scopes.SELF
    member_name: str | None = None
    # V1.5 analytics (validated)
    months: int | None = None
    metric: str | None = None  # MONTHLY_TREND: EXPENSE | INCOME | BALANCE
    direction: str | None = None  # CATEGORY_TREND: INCREASE | DECREASE | ALL
    months_capped: bool = False

    def as_context(self, scope: scopes.ReadScope) -> dict:
        """Validated parameters only (ids, dates, kinds, resolved names) for follow-ups.

        Semantics, never permission: every follow-up is re-authorized from scratch.
        """
        return {
            "scope": scope.kind,
            "target_member_name": scope.member_name,
            "query_kind": self.kind,
            "transaction_type": self.type,
            "category_id": self.category.pk if self.category else None,
            "subcategory_id": self.subcategory.pk if self.subcategory else None,
            "start_date": self.start.isoformat() if self.start else None,
            "end_date": self.end.isoformat() if self.end else None,
            "compare_start_date": self.compare_start.isoformat() if self.compare_start else None,
            "compare_end_date": self.compare_end.isoformat() if self.compare_end else None,
            "limit": self.limit,
            "months": self.months,
            "trend_metric": self.metric,
            "trend_direction": self.direction,
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
    if i.intent in _MUTATIONS and _names_someone_else(i):
        # Owner-only by design (no delegated create/update/undo). The services also enforce
        # it; this gives a clear reply instead of silently acting on the sender's own data.
        reply = (
            messages.OWN_ACCOUNT_ONLY
            if i.intent == "CREATE_TRANSACTION"
            else (messages.OWN_CHANGES_ONLY)
        )
        return ReplyPlan(i.intent, reply, "FORBIDDEN_OTHER_ACCOUNT")
    if i.intent == "CREATE_TRANSACTION":
        return _plan_create(i)
    if i.intent == "QUERY_TRANSACTIONS":
        return _plan_query(i, today)
    if i.intent == "UPDATE_TRANSACTION":
        return _plan_update(i, user, today)
    if i.intent == "UNDO_TRANSACTION":
        return UndoPlan(i.intent)
    return ReplyPlan(i.intent, messages.NOT_UNDERSTOOD, "NOT_UNDERSTOOD")


_MUTATIONS = ("CREATE_TRANSACTION", "UPDATE_TRANSACTION", "UNDO_TRANSACTION")


def _names_someone_else(i: Interpretation) -> bool:
    return i.scope in (scopes.FAMILY, scopes.FAMILY_MEMBER) or bool(i.target_member_name)


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


# --- RECEIPT IMAGE (V1.4) ------------------------------------------------------------------


def plan_receipt(extraction: ReceiptExtraction, *, message_date: date) -> Plan:
    """Validated CreatePlan from a receipt proposal, or a reply explaining why not.

    Creation needs, all checked here (the service re-validates again): a recognized financial
    document, a clear direction (EXPENSE/INCOME), a legible amount, BRL, a valid/fallback
    category and a valid date. Missing printed date → the date the message was received
    (documented rule); an unreadable/invalid printed date is not guessed.
    """
    intent = "CREATE_TRANSACTION"
    e = extraction
    if not e.is_financial_document or e.document_type == "OTHER":
        return ReplyPlan(intent, messages.NOT_A_RECEIPT, "NOT_A_RECEIPT")
    if e.currency == "OTHER":
        return ReplyPlan(intent, messages.RECEIPT_FOREIGN_CURRENCY, "FOREIGN_CURRENCY")
    if e.amount is None:
        return ReplyPlan(intent, messages.RECEIPT_AMOUNT_UNREADABLE, "AMOUNT_UNREADABLE")
    if e.currency != "BRL":
        return ReplyPlan(intent, messages.RECEIPT_CURRENCY_UNCLEAR, "CURRENCY_UNCLEAR")
    if e.transaction_type not in TransactionType.values:
        return ReplyPlan(intent, messages.RECEIPT_DIRECTION_UNCLEAR, "DIRECTION_UNCLEAR")
    try:
        amount = to_decimal(e.amount)
    except InvalidAmount:
        return ReplyPlan(intent, messages.RECEIPT_AMOUNT_UNREADABLE, "AMOUNT_UNREADABLE")

    if e.transaction_date is None:
        transaction_date = message_date
    else:
        transaction_date = _parse_date(e.transaction_date)
        if transaction_date is None:
            return ReplyPlan(intent, messages.RECEIPT_DATE_UNREADABLE, "DATE_UNREADABLE")

    category, subcategory = _resolve_for_create(e.transaction_type, e.category_id, e.subcategory_id)
    return CreatePlan(
        intent=intent,
        type=e.transaction_type,
        amount=amount,  # zero/negative/limit/2-decimals enforced by create_transaction
        category=category,
        subcategory=subcategory,
        description=(e.description or "")[:MAX_DESCRIPTION],
        transaction_date=transaction_date,
    )


# --- QUERY -------------------------------------------------------------------------------


def _plan_query(i: Interpretation, today: date) -> Plan:
    kind = i.query_kind
    transaction_type = i.transaction_type
    if kind == "TOTAL" and transaction_type not in TransactionType.values:
        return ReplyPlan(i.intent, messages.NOT_UNDERSTOOD, "NOT_UNDERSTOOD")
    if kind == "BALANCE":
        transaction_type = None
    elif kind == "TOP":
        # "maiores gastos" (default) or "maior entrada" (INCOME)
        transaction_type = (
            TransactionType.INCOME
            if transaction_type == TransactionType.INCOME
            else TransactionType.EXPENSE
        )
    elif kind in ("BREAKDOWN", "COMPARE", "RANKING", "CATEGORY_TREND"):
        transaction_type = transaction_type or TransactionType.EXPENSE
    elif kind in ("PERIOD_SUMMARY", "MONTHLY_TREND"):
        transaction_type = None
    elif kind not in ("TOTAL", "LIST"):
        return ReplyPlan(i.intent, messages.NOT_UNDERSTOOD, "NOT_UNDERSTOOD")

    period = _period(i.start_date, i.end_date, today, default_month=kind != "LIST")
    if isinstance(period, ReplyPlan):
        return ReplyPlan(i.intent, period.reply, period.code)
    start, end = period

    compare_start = compare_end = None
    if kind in ("COMPARE", "CATEGORY_TREND"):
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

    if kind == "CATEGORY_TREND":
        resolved = _resolve_for_query(i.category_id, None, transaction_type)
        if resolved is None:
            return ReplyPlan(i.intent, messages.CATEGORY_NOT_FOUND, "CATEGORY_NOT_FOUND")
        category = resolved[0]

    limit = None
    if kind in ("TOP", "LIST", "CATEGORY_TREND"):
        limit = DEFAULT_LIMIT if i.limit is None or i.limit < 1 else min(i.limit, MAX_LIMIT)

    months = metric = direction = None
    months_capped = False
    if kind == "MONTHLY_TREND":
        # Untrusted: null/zero/negative → default; above the maximum → capped (and said so).
        months = i.months if isinstance(i.months, int) and i.months >= 1 else None
        months_capped = months is not None and months > analytics.MAX_MONTHS
        months = min(months or analytics.DEFAULT_TREND_MONTHS, analytics.MAX_MONTHS)
        metric = i.trend_metric or i.transaction_type or TransactionType.EXPENSE
    if kind == "CATEGORY_TREND":
        direction = i.trend_direction or analytics.ALL

    scope_kind = i.scope if i.scope in (scopes.FAMILY, scopes.FAMILY_MEMBER) else scopes.SELF
    if scope_kind == scopes.SELF and i.target_member_name:
        scope_kind = scopes.FAMILY_MEMBER  # a named person is never the sender's own data
    if kind == "RANKING":
        scope_kind = scopes.FAMILY  # per-person totals only exist at family level

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
        scope_kind=scope_kind,
        member_name=i.target_member_name if scope_kind == scopes.FAMILY_MEMBER else None,
        months=months,
        metric=metric,
        direction=direction,
        months_capped=months_capped,
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


def execute(
    plan_: Plan,
    *,
    user: User,
    idempotency_key: str,
    today: date,
    source: str = TransactionSource.TEXT,
) -> Outcome:
    """Run the effect for ``user`` (the authenticated sender). Call inside transaction.atomic."""
    if isinstance(plan_, ReplyPlan):
        return Outcome(plan_.intent, plan_.reply, plan_.code)
    if isinstance(plan_, CreatePlan):
        return _execute_create(plan_, user, idempotency_key, today, source)
    if isinstance(plan_, UpdatePlan):
        return _execute_update(plan_, user, idempotency_key)
    if isinstance(plan_, UndoPlan):
        return _execute_undo(plan_, user, idempotency_key)
    return _execute_query(plan_, user, today)


def _execute_create(
    p: CreatePlan, user: User, idempotency_key: str, today: date, source: str
) -> Outcome:
    try:
        txn = create_transaction(
            user=user,
            type=p.type,
            amount=p.amount,
            category=p.category,
            subcategory=p.subcategory,
            description=p.description,
            transaction_date=p.transaction_date,
            source=source,  # audit only: TEXT or AUDIO, same rules either way
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


def _authorize(p: QueryPlan, user: User) -> scopes.ReadScope | Outcome:
    """Turn the untrusted scope hint into an authorized ReadScope, or a discreet refusal.

    Runs inside the message's atomic block, so the actor's role is checked at execution time
    (a stored follow-up context never carries permission).
    """
    try:
        return scopes.resolve(user, p.scope_kind, p.member_name)
    except scopes.ScopeDenied as denied:
        if denied.code == denied.MEMBER_AMBIGUOUS:
            reply = messages.member_ambiguous(denied.name)
        else:
            reply = {
                denied.NO_FAMILY: messages.NO_FAMILY,
                denied.ADMIN_ONLY: messages.ADMIN_ONLY,
                denied.MEMBER_NOT_FOUND: messages.MEMBER_NOT_FOUND,
            }[denied.code]
        logger.info("query.scope_denied scope=%s reason=%s", p.scope_kind, denied.code)
        return Outcome(p.intent, reply, denied.code)


def _execute_query(p: QueryPlan, user: User, today: date) -> Outcome:
    scope = _authorize(p, user)
    if isinstance(scope, Outcome):
        return scope
    is_income = p.type == TransactionType.INCOME
    is_family = scope.kind == scopes.FAMILY
    who = (
        None if scope.kind == scopes.SELF else (messages.FAMILY if is_family else scope.member_name)
    )
    context = p.as_context(scope)

    if p.kind == "PERIOD_SUMMARY":
        summary = analytics.period_summary(scope=scope, start_date=p.start, end_date=p.end)
        reply = messages.period_summary(summary, start=p.start, end=p.end, today=today, who=who)
    elif p.kind == "MONTHLY_TREND":
        end_month = min(p.end or today, today)
        trend = analytics.monthly_trend(
            scope=scope, months=p.months, end_month=end_month, metric=p.metric
        )
        reply = messages.monthly_trend(trend, capped=p.months_capped, who=who)
    elif p.kind == "CATEGORY_TREND":
        changes = analytics.category_trend(
            scope=scope,
            type=p.type,
            start_date=p.start,
            end_date=p.end,
            compare_start_date=p.compare_start,
            compare_end_date=p.compare_end,
            direction=p.direction,
            category_id=p.category.pk if p.category else None,
            limit=p.limit,
        )
        reply = messages.category_trend(
            changes,
            direction=p.direction,
            is_income=is_income,
            period_a=(p.start, p.end),
            period_b=(p.compare_start, p.compare_end),
            today=today,
            who=who,
        )
    elif p.kind == "RANKING":
        rows = selectors.get_member_totals(
            scope=scope, start_date=p.start, end_date=p.end, type=p.type
        )
        reply = messages.ranking(
            [(r.name, r.total) for r in rows],
            is_income=is_income,
            start=p.start,
            end=p.end,
            today=today,
        )
    elif p.kind == "BALANCE":
        totals = selectors.get_period_totals(scope=scope, start_date=p.start, end_date=p.end)
        reply = messages.balance(
            income=totals.income,
            expense=totals.expense,
            start=p.start,
            end=p.end,
            today=today,
            who=who,
        )
    elif p.kind == "TOP":
        rows = selectors.get_top_expenses(
            scope=scope, start_date=p.start, end_date=p.end, limit=p.limit, type=p.type
        )
        reply = messages.top_expenses(
            [(_item_label(t, with_member=is_family), t.amount) for t in rows],
            start=p.start,
            end=p.end,
            today=today,
            who=who,
            is_income=is_income,
        )
    elif p.kind == "BREAKDOWN":
        rows = selectors.get_totals_by_category(
            scope=scope, start_date=p.start, end_date=p.end, type=p.type
        )
        reply = messages.breakdown(
            [(r.category_name, r.total) for r in rows],
            is_income=is_income,
            start=p.start,
            end=p.end,
            today=today,
            who=who,
        )
    elif p.kind == "LIST":
        rows = selectors.list_transactions(
            scope=scope,
            start_date=p.start,
            end_date=p.end,
            type=p.type,
            category=p.category,
            subcategory=p.subcategory,
        )[: p.limit]
        reply = messages.listing(
            [
                (
                    t.transaction_date,
                    _item_label(t, with_member=is_family),
                    t.amount,
                    t.type == TransactionType.INCOME,
                )
                for t in rows
            ],
            type_=p.type,
            period=(p.start, p.end) if p.start else None,
            today=today,
            who=who,
        )
    elif p.kind == "COMPARE":
        result = selectors.compare_periods(
            scope=scope,
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
            who=who,
        )
    else:  # TOTAL
        value = selectors.get_total(
            scope=scope,
            start_date=p.start,
            end_date=p.end,
            type=p.type,
            category=p.category,
            subcategory=p.subcategory,
        )
        reply = messages.total(
            is_income=is_income,
            value=value,
            scope=_scope(p),
            start=p.start,
            end=p.end,
            today=today,
            who=who,
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


def _item_label(txn: Transaction, *, with_member: bool = False) -> str:
    if txn.description:
        label = txn.description[:1].upper() + txn.description[1:]
    else:
        label = txn.subcategory.name if txn.subcategory else txn.category.name
    if with_member:
        label += f" ({member_display_name(txn.user)})"
    return label


def _rejected(intent: str, exc: ValidationError) -> Outcome:
    field_name, reason = _first_error(exc)
    return Outcome(intent, messages.rejected(reason), f"REJECTED_{field_name.upper()}"[:50])


def _first_error(exc: ValidationError) -> tuple[str, str]:
    if hasattr(exc, "message_dict"):
        for field_name, errors in exc.message_dict.items():
            if errors:
                return field_name, errors[0]
    return "invalid", exc.messages[0] if exc.messages else "dados inválidos."
