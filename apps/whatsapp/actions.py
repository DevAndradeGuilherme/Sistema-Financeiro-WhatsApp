"""Turn an untrusted ``Interpretation`` into a validated plan and execute it.

Django is the authority here:
- ids proposed by the model are re-checked against the catalog (active, compatible type);
- amounts/dates are parsed locally (Decimal, ISO dates) and re-validated by the domain service;
- the user is always the authenticated sender passed by the caller — never a model field;
- family reads (V1.2) go through ``apps.transactions.scopes``: the model's ``scope`` /
  ``target_member_name`` are hints; authorization (ADMIN of the *current* family, member
  resolved by name inside that family) happens at execution time; mutations are owner-only
  and a creation always belongs to the sender (a named person is never its owner); an
  edit/removal whose text names another family member is refused, whatever the model says;
- the transaction to edit/remove is chosen by Django in the sender's own data (never by an
  id from the model), combining every criterion stated with AND; a criterion the text states
  but the model dropped or contradicted (``hints``) → asks; 0 candidates → "não encontrei",
  2+ → asks; "the latest" only when the text says "último"/"acabei de" — a bare "desfazer"
  identifies nothing and changes nothing; several on purpose ("todas as entradas") →
  confirmation of the exact set first; a correction ("errado, foram 80") never creates and only
  edits the effect of the sender's immediately previous message;
- totals, rankings, groupings and percentages come only from
  ``apps.transactions.selectors`` (PostgreSQL / Decimal).

``plan`` only reads and validates *criteria* (never picks a transaction). ``execute`` runs
inside the caller's atomic block **after the sender's lock** (see ``pipeline.commit_plan``):
it re-reads the conversation (pending action, previous message), selects the candidates with
``SELECT ... FOR UPDATE`` and performs the effect, so selection and effect see the same state.
"""

import logging
import re
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
from apps.families.selectors import member_display_name, normalize_name, other_member_names
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
    soft_delete_transaction,
    soft_delete_transactions,
    undo_last_change,
    update_transaction,
)
from common.formatting import describe_day, describe_period
from common.money import InvalidAmount, to_decimal

from . import context as conversation
from . import hints, messages, pending
from .models import InboundMessage

logger = logging.getLogger(__name__)

MAX_DESCRIPTION = 255
DEFAULT_LIMIT = 5
MAX_LIMIT = 10
REFERENCE_WINDOW_DAYS = 90  # how far back "o gasto de gasolina" is searched (no date given)
MAX_CANDIDATES_SHOWN = 3  # ambiguous reference: candidates listed in the reply
MAX_BULK = 50  # most transactions a single confirmed request may remove
ANSWER_INTENT = "CONFIRM_ACTION"  # "sim"/"não" to a pending action (never sent to the model)


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
    # Text is only an amount + filler ("80", "foi 80"): refused right after a mutation.
    elliptic: bool = False


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
class _Target:
    """Validated description of the wanted transaction(s). ``filters`` = find_transactions.

    Every filter is combined with AND; none is ever dropped to find something.
    """

    filters: dict
    last: bool = False
    bulk: bool = False
    # At least one criterion is proven by the text itself (type word, number, date, catalog
    # item, description words), not only proposed by the model.
    anchored: bool = False
    # Numbers of the text that are neither a criterion nor a new value ("foi 80, não 100").
    leftover_amounts: frozenset = frozenset()
    # Some criterion other than the type is proven by the text: a type alone ("a entrada")
    # never identifies the one transaction to remove.
    identified: bool = False

    @property
    def identifying(self) -> set:
        return self.filters.keys() - {"type"}

    @property
    def dated(self) -> bool:
        return bool({"transaction_date", "start_date", "end_date"} & self.filters.keys())

    def criteria(self) -> dict:
        """JSON form of the approved filters (stored with a pending confirmation)."""
        data = {}
        for name, value in self.filters.items():
            if isinstance(value, Category | Subcategory):
                data[f"{name}_id"] = value.pk
            elif isinstance(value, date):
                data[name] = value.isoformat()
            elif isinstance(value, Decimal):
                data[name] = str(value)
            else:
                data[name] = value
        return data

    @classmethod
    def from_criteria(cls, data: dict) -> "_Target | None":
        """Inverse of ``criteria``; None if something no longer resolves (→ stale)."""
        filters: dict = {}
        for name, value in data.items():
            if name == "category_id":
                filters["category"] = Category.objects.filter(pk=value).first()
            elif name == "subcategory_id":
                filters["subcategory"] = Subcategory.objects.filter(pk=value).first()
            elif name in ("transaction_date", "start_date", "end_date"):
                filters[name] = _parse_date(value)
            elif name == "amount":
                filters[name] = Decimal(value)
            elif name in ("type", "description"):
                filters[name] = value
            else:
                return None
        if any(value is None for value in filters.values()):
            return None
        return cls(filters=filters, bulk=True)


@dataclass(frozen=True)
class UpdatePlan:
    """Edit one of the sender's transactions, chosen at execution under the sender's lock.

    ``target`` None = context-only correction ("errado, foram 80"): only the effect of the
    sender's immediately previous message, of ``wanted_type`` if the text names one.
    """

    intent: str
    target: _Target | None
    changes: dict = field(default_factory=dict)  # only fields to change (service kwargs)
    wanted_type: str | None = None
    previous_amount: Decimal | None = None  # "foi 80, não 100": the context must be 100 now


@dataclass(frozen=True)
class UndoPlan:
    """Undo the sender's last action (stack, ``undo_last_change``).

    Only when the text itself says so ("desfaz a última alteração"); never a bare "desfazer".
    """

    intent: str


@dataclass(frozen=True)
class DeletePlan:
    """Soft delete exactly one transaction, resolved at execution in the sender's own data."""

    intent: str
    target: _Target


@dataclass(frozen=True)
class BulkDeletePlan:
    """Several transactions: nothing is deleted now; the exact set waits for "sim"."""

    intent: str
    target: _Target


@dataclass(frozen=True)
class AnswerPlan:
    """A whole-message "sim"/"não" (``pending.CONFIRM``/``DENY``) to the pending action."""

    intent: str
    decision: str


Plan = (
    ReplyPlan
    | CreatePlan
    | QueryPlan
    | UpdatePlan
    | UndoPlan
    | DeletePlan
    | BulkDeletePlan
    | AnswerPlan
)


@dataclass(frozen=True)
class Outcome:
    intent: str
    reply: str
    code: str = ""  # non-empty: answered with a rejection / no effect
    transaction: Transaction | None = None
    query_context: dict | None = None  # to remember for follow-ups


def plan(
    interpretation: Interpretation,
    *,
    user: User,
    today: date,
    has_context: bool = False,
    text: str = "",
) -> Plan:
    i = interpretation
    if i.follow_up and not has_context:
        return ReplyPlan(i.intent, messages.NO_CONTEXT, "NO_CONTEXT")
    if i.intent == "HELP":
        return ReplyPlan(i.intent, messages.HELP)
    said = hints.read(text)
    if i.intent == "CREATE_TRANSACTION":
        # The owner is always the sender (``execute`` receives it; CreatePlan has no owner).
        # ``scope``/``target_member_name`` are ignored here: a name is usually a beneficiary
        # ("curso da Ana"). An explicit booking for someone else is refused, not silently
        # recorded for the sender — flagged by the model or detected in the text by Django.
        if i.for_another_person or _books_for_member(text, user):
            return ReplyPlan(i.intent, messages.OWN_ACCOUNT_ONLY, "FORBIDDEN_OTHER_ACCOUNT")
        if said.mutation == "REMOVE":
            # "Remova a entrada de 50" proposed as CREATE: an explicit order to remove is
            # never a new registration, whatever the model said (incompatible intent).
            return ReplyPlan(i.intent, messages.REMOVE_NOT_CREATED, "TARGET_UNCLEAR")
        if i.correction or said.correction or said.mutation == "EDIT":
            # "Errado, recebi somente 1.050,41", "desculpa, foram 80", "altere o valor": an
            # amount in a correction is never a new transaction (model or Django flagged it).
            return ReplyPlan(i.intent, messages.CORRECTION_NOT_CREATED, "CORRECTION_NOT_CREATED")
        return _plan_create(i, elliptic=said.elliptic)
    if i.intent in _OWNER_EDITS and (_names_someone_else(i) or _mentions_member(text, user)):
        # Owner-only by design (no delegated update/undo), also for a family ADMIN. A member
        # named in the text blocks the mutation even if the model left the name out: acting on
        # the sender's own look-alike transaction ("a entrada de 77 da Maria") would be wrong.
        return ReplyPlan(i.intent, messages.OWN_CHANGES_ONLY, "FORBIDDEN_OTHER_ACCOUNT")
    if i.intent == "QUERY_TRANSACTIONS":
        return _plan_query(i, today)
    if i.intent == "UPDATE_TRANSACTION":
        return _plan_update(i, said, text, today)
    if i.intent == "UNDO_TRANSACTION":
        return _plan_undo(i, said, text, today)
    return ReplyPlan(i.intent, messages.NOT_UNDERSTOOD, "NOT_UNDERSTOOD")


_OWNER_EDITS = ("UPDATE_TRANSACTION", "UNDO_TRANSACTION")


def _names_someone_else(i: Interpretation) -> bool:
    return i.scope in (scopes.FAMILY, scopes.FAMILY_MEMBER) or bool(i.target_member_name)


def _mentions_member(text: str, user: User) -> bool:
    """The text names another member of the sender's family (first/full name, accent-free)."""
    names = other_member_names(user=user) if text else set()
    return bool(names) and bool(hints.find_terms(text, names))


# Imperative "record it" verbs (registra, lança, anota...), never first-person past ("comprei").
_RECORD_VERB = r"\b(?:registr|lanc|anot|adicion|coloc|inclu)(?:a|e|ar|ir)\b"
_THIRD_PERSON = r"(?:gastou|recebeu|pagou|comprou|ganhou|transferiu)"


def _books_for_member(text: str, user: User) -> bool:
    """Deterministic backstop for ``for_another_person``: the text explicitly books money to
    another member of the sender's family ("registra 100 para a Ana", "lança na conta da Ana",
    "Ana gastou 100"). A beneficiary mention ("curso da Ana", "presente pra Ana") never matches.
    """
    names = other_member_names(user=user) if text else set()
    if not names:
        return False
    alt = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    name = rf"(?:[ao]\s+)?(?:{alt})\b"
    normalized = normalize_name(text)
    return any(
        re.search(pattern, normalized)
        for pattern in (
            rf"{_RECORD_VERB}.*\b(?:para|pra|pro)\s+{name}",
            rf"\bconta\s+d[aeo]\s+{name}",
            rf"\b(?:{alt})\s+{_THIRD_PERSON}\b",
        )
    )


# --- CREATE ------------------------------------------------------------------------------


def _plan_create(i: Interpretation, *, elliptic: bool = False) -> Plan:
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
        elliptic=elliptic,
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

    months = metric = direction = None
    months_capped = False
    if kind == "MONTHLY_TREND":
        # ``months`` defines the window width. The optional end date only anchors its final
        # month; a model-proposed start date is irrelevant and must not invalidate the query.
        months = i.months if isinstance(i.months, int) and i.months >= 1 else None
        months_capped = months is not None and months > analytics.MAX_MONTHS
        months = min(months or analytics.DEFAULT_TREND_MONTHS, analytics.MAX_MONTHS)
        metric = i.trend_metric or i.transaction_type or TransactionType.EXPENSE
        period = _monthly_period(i.end_date, today, months)
    else:
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


def _monthly_period(end_raw, today: date, months: int):
    """Effective monthly window: ``months`` wins; ``end_raw`` only chooses the final month.

    The model may emit an unrelated one-day ``start_date`` alongside ``months``. Ignoring that
    start date prevents it from rejecting or narrowing the trend and keeps stored context
    aligned with the window that PostgreSQL actually reads.
    """
    end_hint = _parse_date(end_raw) if end_raw else today
    if end_hint is None:
        return ReplyPlan("", messages.INVALID_DATE, "INVALID_DATE")
    anchor = min(end_hint, today)
    start = analytics.month_window(months, anchor)[0]
    if (anchor.year, anchor.month) == (today.year, today.month):
        end = today
    else:
        next_month = (anchor.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = next_month - timedelta(days=1)
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


# --- UPDATE / UNDO: criteria (validated here, resolved at execution) ---------------------


def _plan_update(i: Interpretation, said: hints.TextHints, text: str, today: date) -> Plan:
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

    # Numbers/dates/items in an UPDATE text are either criteria or new values: each one must
    # be in the reference or in the updates (``_read_target``), never silently ignored.
    target = _read_target(i.reference, said, text, today, removing=False, changes=changes)
    if isinstance(target, ReplyPlan):
        return ReplyPlan(i.intent, target.reply, target.code)
    unclear = ReplyPlan(i.intent, messages.CORRECTION_TARGET_UNCLEAR, "TARGET_UNCLEAR")
    if target.bulk:
        return ReplyPlan(i.intent, messages.BULK_UPDATE_UNSUPPORTED, "BULK_UPDATE_UNSUPPORTED")
    if target.last or target.identifying:
        # A type alone ("a entrada") never identifies the transaction, and a criterion only the
        # model proposed (an amount, date, item or description the text does not state as the
        # target) can never be the reason one is chosen: "último" or a criterion beyond the
        # type proven by the text is required.
        if target.leftover_amounts or not (target.last or target.identified):
            return unclear
        return UpdatePlan(i.intent, target, changes)
    # No criteria ("errado, foram 80", "muda para 12"): only the effect of the sender's
    # immediately previous message (checked at execution, under the lock). The new value must
    # be in the text; a second number can only be the value being corrected ("não 100").
    if len(target.leftover_amounts) > 1 or not _changes_anchored(said, text, changes):
        return unclear
    return UpdatePlan(
        i.intent,
        None,
        changes,
        wanted_type=target.filters.get("type"),
        previous_amount=next(iter(target.leftover_amounts), None),
    )


def _plan_undo(i: Interpretation, said: hints.TextHints, text: str, today: date) -> Plan:
    target = _read_target(i.reference, said, text, today, removing=True)
    if isinstance(target, ReplyPlan):
        return ReplyPlan(i.intent, target.reply, target.code)
    if target.leftover_amounts:
        return ReplyPlan(i.intent, messages.REMOVE_TARGET_UNCLEAR, "TARGET_UNCLEAR")
    if target.bulk:
        if not (target.filters and target.anchored):
            return ReplyPlan(i.intent, messages.BULK_NEEDS_CRITERIA, "BULK_NEEDS_CRITERIA")
        return BulkDeletePlan(i.intent, target)
    if target.last or (target.identifying and target.identified):
        return DeletePlan(i.intent, target)
    if said.last and said.action:
        # "Desfaz a última alteração": the text itself asks for the last action (stack).
        return UndoPlan(i.intent)
    if said.type is not None:
        # "Desfaça a entrada", "apague o gasto": a type alone identifies nothing, even when
        # only one such transaction exists → ask which one.
        return ReplyPlan(i.intent, messages.remove_which(said.type), "TARGET_UNCLEAR")
    # "desfazer", "remove", "apaga": nothing in the text identifies a transaction → nothing
    # changes, whatever the model proposed (``last``, a type, a description...).
    return ReplyPlan(i.intent, messages.REMOVE_TARGET_UNCLEAR, "TARGET_UNCLEAR")


def _read_target(
    reference: TransactionReference | None,
    said: hints.TextHints,
    text: str,
    today: date,
    *,
    removing: bool,
    changes: dict | None = None,
) -> _Target | ReplyPlan:
    """Every criterion of the model's reference, checked against what the text itself says.

    All criteria are kept and combined with AND (a description is kept next to a category).
    A type named in the text is enforced. An amount, date or catalog item stated in the text
    but missing from (or contradicted by) the proposal — reference, plus ``changes`` for an
    UPDATE — means the model lost a filter → ask, instead of degrading to a broader search.
    An amount or date *criterion* the text does not show is refused too. The caller decides
    with ``anchored`` (some criterion is proven by the text) and ``leftover_amounts``.
    """
    reference = reference or TransactionReference()
    changes = changes or {}
    unclear = ReplyPlan(
        "",
        messages.REMOVE_TARGET_UNCLEAR if removing else messages.CORRECTION_TARGET_UNCLEAR,
        "TARGET_UNCLEAR",
    )
    filters: dict = {}
    if reference.transaction_type in TransactionType.values:
        filters["type"] = reference.transaction_type
    if reference.category_id is not None:
        filters["category"] = Category.objects.filter(pk=reference.category_id).first()
    if reference.subcategory_id is not None:
        filters["subcategory"] = Subcategory.objects.filter(pk=reference.subcategory_id).first()
    if reference.description and reference.description.strip():
        filters["description"] = reference.description.strip()[:MAX_DESCRIPTION]
    if reference.amount is not None:
        try:
            filters["amount"] = to_decimal(reference.amount)
        except InvalidAmount:
            filters["amount"] = None
    if reference.transaction_date is not None:
        filters["transaction_date"] = _parse_date(reference.transaction_date)
    period = _reference_period(reference)
    if isinstance(period, ReplyPlan):
        return period
    filters.update(period)

    # A criterion the model sent but that does not resolve can only mean "not found".
    if any(value is None for value in filters.values()):
        return ReplyPlan("", messages.NOT_FOUND, "NOT_FOUND")

    if said.type is not None and filters.setdefault("type", said.type) != said.type:
        return unclear
    # Every date/period of the text, resolved ("dia 5" → that day, "outubro" → that month).
    # One that cannot be resolved safely → ask; never "the text has some date, accept any".
    when = hints.temporal(text, today)
    reference_spans = _reference_spans(said, today)
    if when.unresolved or reference_spans is None:
        return unclear
    items = _catalog_mentions(text)
    leftover = _leftover_amounts(said, filters, changes)
    if (
        leftover is None
        or not _dates_match(when.spans, reference_spans, filters, changes, today)
        or not _items_match(items, filters, changes)
    ):
        return unclear

    bulk = reference.all or said.bulk
    # "o último" only when the text says so, and never to break a tie between explicit
    # criteria: a model "last=true" alone cannot turn a search into "the latest one".
    # "a última alteração" (no criteria) is the undo stack, not "the latest transaction".
    last = (
        said.last
        and not bulk
        and not (said.action and not filters)
        and (reference.last or not (filters.keys() - {"type"}))
    )
    dated_by_text = bool(when.spans) and (
        removing or bool(reference_spans) or not changes.get("transaction_date")
    )
    proven = _proven(said, text, items, filters, changes, dated_by_text=dated_by_text)
    if last and filters.keys() - {"type"} - proven:
        # "a última entrada" + a filter only the model proposed would pick "the latest one
        # matching the model", not the one the text names → ask.
        return unclear
    return _Target(
        filters=filters,
        last=last,
        bulk=bulk,
        anchored=_anchored(said, text, items, filters),
        leftover_amounts=leftover,
        identified=bool(proven - {"type"}),
    )


def _leftover_amounts(said: hints.TextHints, filters: dict, changes: dict) -> frozenset | None:
    """Numbers of the text that are neither the amount criterion nor the new amount.

    None (→ ask) when a number of the text is unreadable or when the amount criterion is not a
    number the text shows (spelled-out numbers cannot be checked, so they are accepted).
    """
    if any(amount is None for amount in said.amounts):
        return None
    if said.amounts and filters.get("amount") not in (None, *said.amounts):
        return None
    proposed = {filters.get("amount"), changes.get("amount")} - {None}
    return frozenset(amount for amount in said.amounts if amount not in proposed)


def _anchored(said: hints.TextHints, text: str, items: dict, filters: dict) -> bool:
    """Some criterion of ``filters`` is proven by the text, not only proposed by the model."""
    if said.type is not None and filters.get("type") == said.type:
        return True
    if filters.get("amount") is not None and filters["amount"] in said.amounts:
        return True
    if said.date and {"transaction_date", "start_date", "end_date"} & filters.keys():
        return True  # ``_dates_match`` already proved explicit days agree
    if _names_item(items, filters):
        return True
    return _words_in_text(filters.get("description"), text)


_DATE_FILTERS = frozenset({"transaction_date", "start_date", "end_date"})


def _proven(
    said: hints.TextHints,
    text: str,
    items: dict,
    filters: dict,
    changes: dict,
    *,
    dated_by_text: bool,
) -> set:
    """Names of the filters the text itself proves *as a criterion* (not only as a new value).

    Only these can be the reason a transaction is selected; the rest, proposed by the model,
    may at most narrow a set the text already identifies. A token of the text used as the new
    value ("altere a entrada para 50", "... para bônus") does not also prove the criterion.
    """
    proven = set()
    if said.type is not None and filters.get("type") == said.type:
        proven.add("type")
    amount = filters.get("amount")
    if amount is not None:
        used_as_new = 1 if changes.get("amount") == amount else 0
        if said.amounts.count(amount) > used_as_new:
            proven.add("amount")
    if dated_by_text:
        proven |= _DATE_FILTERS & filters.keys()  # ``_dates_match`` proved they fit the text
    for kind in ("category", "subcategory"):
        chosen = filters.get(kind)
        if chosen is None or (changes.get(kind) is not None and changes[kind].pk == chosen.pk):
            continue
        if any(k == kind and pk == chosen.pk for k, pk in items.values()):
            proven.add(kind)
    described = hints.descriptive_words(filters.get("description"))
    if (described & hints.descriptive_words(text)) - hints.descriptive_words(
        changes.get("description")
    ):
        proven.add("description")
    return proven


def _changes_anchored(said: hints.TextHints, text: str, changes: dict) -> bool:
    """Some new value of a context-only correction is stated in the text."""
    if changes.get("amount") is not None and changes["amount"] in said.amounts:
        return True
    if said.date and changes.get("transaction_date"):
        return True
    if _names_item(_catalog_mentions(text), changes):
        return True
    return _words_in_text(changes.get("description"), text)


def _names_item(items: dict, values: dict) -> bool:
    for kind, pk in items.values():
        chosen = values.get(kind)
        if chosen is not None and chosen.pk == pk:
            return True
    return False


def _words_in_text(phrase: str | None, text: str) -> bool:
    normalized = hints.normalize(text)
    return any(word in normalized for word in hints.normalize(phrase).split() if len(word) >= 3)


def _reference_spans(said: hints.TextHints, today: date) -> list | None:
    """Spans of the dates that identify the transaction ("a entrada do dia 5"); None if one
    of them cannot be resolved."""
    spans = []
    for fragment in said.reference_dates:
        found = hints.temporal(fragment, today)
        if found.unresolved or not found.spans:
            return None
        spans.extend(found.spans)
    return spans


def _target_window(filters: dict, today: date) -> tuple[date, date] | None:
    """The ``transaction_date`` range the proposed criteria select (AND), or None if undated.
    A missing start is unbounded (``find_transactions`` reads only ``<= end``)."""
    if not _DATE_FILTERS & filters.keys():
        return None
    point, start, end = (
        filters.get(name) for name in ("transaction_date", "start_date", "end_date")
    )
    lows = [d for d in (point, start) if d]
    highs = [d for d in (point, end) if d] or [max(start, today)]
    return (max(lows) if lows else date.min), min(highs)


def _within(window: tuple[date, date], span: tuple[date, date]) -> bool:
    return span[0] <= window[0] and window[1] <= span[1]


def _dates_match(
    spans: tuple, reference_spans: list, filters: dict, changes: dict, today: date
) -> bool:
    """The proposal's dates must be the ones the text states — compared by value.

    - a date criterion must lie inside a date/period of the text ("dia 5" → that day only);
    - every date/period of the text must be the criterion or the new date (UPDATE): the
      target window inside it, or the new date in it — never silently ignored;
    - a date that identifies the transaction ("a entrada do dia 5", "o gasto de ontem era
      40") must be the criterion, not only the new date.
    """
    window = _target_window(filters, today)
    new_date = changes.get("transaction_date")
    if window is not None and not any(_within(window, span) for span in spans):
        return False
    for span in spans:
        target_here = window is not None and _within(window, span)
        new_here = new_date is not None and span[0] <= new_date <= span[1]
        if not (target_here or new_here):
            return False
    return not reference_spans or (
        window is not None and all(_within(window, span) for span in reference_spans)
    )


def _catalog_mentions(text: str) -> dict[str, tuple[str, int]]:
    """Unambiguous catalog terms named in the text → ("category"|"subcategory", id)."""
    terms = _catalog_terms()
    return {term: terms[term] for term in hints.find_terms(text, terms)} if terms else {}


def _items_match(items: dict, filters: dict, changes: dict) -> bool:
    """A catalog category/subcategory named in the text must be the proposed one (criterion or
    new value) or appear in a proposed description. Only unambiguous catalog terms count."""
    if not items:
        return True
    subcategories = [s for s in (filters.get("subcategory"), changes.get("subcategory")) if s]
    categories = {c.pk for c in (filters.get("category"), changes.get("category")) if c}
    categories |= {s.category_id for s in subcategories}
    described = " ".join(d for d in (filters.get("description"), changes.get("description")) if d)
    for term, (kind, pk) in items.items():
        if hints.find_terms(described, [term]):
            continue
        if kind == "category" and pk in categories:
            continue
        if kind == "subcategory" and pk in {s.pk for s in subcategories}:
            continue
        return False
    return True


def _catalog_terms() -> dict[str, tuple[str, int]]:
    """Normalized active category/subcategory names and aliases → ("category"|"subcategory",
    id), keeping only terms that are reliable: one catalog entry, 3+ letters, no digits, not a
    type word ("compras")."""
    entries: dict[str, set] = {}
    for category in list_active_categories():
        for term in (category.name, *category.aliases):
            entries.setdefault(hints.normalize(term), set()).add(("category", category.pk))
        for sub in category.active_subcategories:
            for term in (sub.name, *sub.aliases):
                entries.setdefault(hints.normalize(term), set()).add(("subcategory", sub.pk))
    return {
        term: next(iter(found))
        for term, found in entries.items()
        if len(found) == 1
        and len(term) >= 3
        and not any(char.isdigit() for char in term)
        and not hints.names_a_type(term)
    }


def _reference_period(reference: TransactionReference) -> dict | ReplyPlan:
    period = {}
    for name in ("start_date", "end_date"):
        raw = getattr(reference, name)
        if raw is not None:
            period[name] = _parse_date(raw)
            if period[name] is None:
                return ReplyPlan("", messages.INVALID_DATE, "INVALID_DATE")
    start, end = period.get("start_date"), period.get("end_date")
    if start and end and start > end:
        return ReplyPlan("", messages.INVALID_PERIOD, "INVALID_PERIOD")
    return period


def _pick(target: _Target, user: User, today: date, *, verb: str) -> Transaction | ReplyPlan:
    """Exactly one of the sender's transactions matching ALL criteria, or a reply.

    Runs at execution, after the sender's lock; candidates are locked (``FOR UPDATE``).
    """
    if target.last:
        # Explicitly relative: the most recently registered (created_at), of the stated type.
        rows = selectors.find_transactions(user=user, limit=1, for_update=True, **target.filters)
        return rows[0] if rows else ReplyPlan("", messages.NOT_FOUND, "NOT_FOUND")
    since = None if target.dated else today - timedelta(days=REFERENCE_WINDOW_DAYS)
    rows = selectors.find_transactions(
        user=user, since=since, limit=MAX_CANDIDATES_SHOWN + 1, for_update=True, **target.filters
    )
    if not rows:
        return ReplyPlan("", _not_found(target, today), "NOT_FOUND")
    if len(rows) > 1:
        candidates = [
            (t.type == TransactionType.INCOME, t.amount, t.transaction_date, _item_label(t))
            for t in rows[:MAX_CANDIDATES_SHOWN]
        ]
        reply = messages.ambiguous_target(
            candidates, more=len(rows) > MAX_CANDIDATES_SHOWN, verb=verb
        )
        return ReplyPlan("", reply, "AMBIGUOUS_REFERENCE")
    return rows[0]


def _not_found(target: _Target, today: date) -> str:
    if not target.identifying:
        return messages.NOT_FOUND
    return messages.target_not_found(target.filters.get("type"), **_details(target, today))


def _details(target: _Target, today: date) -> dict:
    f = target.filters
    item = f.get("subcategory") or f.get("category")
    when = None
    if f.get("transaction_date"):
        day = f["transaction_date"]
        when = describe_day(day, today) if day >= today - timedelta(days=1) else f"em {day:%d/%m}"
    elif f.get("start_date") or f.get("end_date"):
        start = f.get("start_date") or f["end_date"]
        when = describe_period(start, f.get("end_date") or max(start, today), today)
    return {
        "amount": f.get("amount"),
        "category": item.name if item else None,
        "description": f.get("description"),
        "when": when,
    }


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
    message: InboundMessage,
    idempotency_key: str,
    today: date,
    source: str = TransactionSource.TEXT,
) -> Outcome:
    """Run the effect for ``user`` (the authenticated sender of ``message``).

    Call inside transaction.atomic, after ``lock_user(user)`` (``pipeline.commit_plan``):
    conversation state is re-read and targets are selected here, under that lock.
    """
    if isinstance(plan_, AnswerPlan):
        return _execute_answer(plan_, user, message, idempotency_key)
    # Any other message is a new request: an unanswered confirmation is cancelled, so a later
    # "sim" can never confirm an old question (``pending.answers`` enforces it as well).
    pending.clear(user)
    if isinstance(plan_, ReplyPlan):
        return Outcome(plan_.intent, plan_.reply, plan_.code)
    if isinstance(plan_, CreatePlan):
        return _execute_create(plan_, user, message, idempotency_key, today, source)
    if isinstance(plan_, UpdatePlan):
        return _execute_update(plan_, user, message, idempotency_key, today)
    if isinstance(plan_, UndoPlan):
        return _execute_undo(plan_, user, idempotency_key)
    if isinstance(plan_, DeletePlan):
        return _execute_delete(plan_, user, idempotency_key, today)
    if isinstance(plan_, BulkDeletePlan):
        return _execute_ask(plan_, user, message, idempotency_key, today)
    return _execute_query(plan_, user, today)


def _execute_create(
    p: CreatePlan,
    user: User,
    message: InboundMessage,
    idempotency_key: str,
    today: date,
    source: str,
) -> Outcome:
    if p.elliptic and conversation.recent_mutation(message):
        # "80" / "ok, 80" after a recent registration/edit is most likely a correction, with no
        # evidence of a new transaction: create nothing and ask which one — also when other
        # messages came in between (the link is broken, the intent to correct is not).
        return Outcome(p.intent, messages.CORRECTION_NOT_CREATED, "CORRECTION_NOT_CREATED")
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

    return Outcome(p.intent, created_reply(txn), transaction=txn)


def created_reply(txn: Transaction) -> str:
    """Registration card, derived only from the persisted transaction (also used by recovery)."""
    return messages.transaction_created(
        is_income=txn.type == TransactionType.INCOME,
        amount=txn.amount,
        category=txn.category.name,
        subcategory=txn.subcategory.name if txn.subcategory else None,
        transaction_date=txn.transaction_date,
    )


def _execute_update(
    p: UpdatePlan, user: User, message: InboundMessage, idempotency_key: str, today: date
) -> Outcome:
    if p.target is None:
        txn = conversation.correction_target(message)
        if (
            txn is None
            or (p.wanted_type is not None and txn.type != p.wanted_type)
            or (p.previous_amount is not None and txn.amount != p.previous_amount)
        ):
            return Outcome(
                p.intent, messages.CORRECTION_TARGET_UNCLEAR, "CORRECTION_TARGET_UNCLEAR"
            )
    else:
        txn = _pick(p.target, user, today, verb="corrigir")
        if isinstance(txn, ReplyPlan):
            return Outcome(p.intent, txn.reply, txn.code)
    kwargs = {name: p.changes.get(name, UNSET) for name in _UPDATABLE}
    try:
        result = update_transaction(
            user=user, transaction_id=txn.pk, origin=idempotency_key, **kwargs
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


def _execute_delete(p: DeletePlan, user: User, idempotency_key: str, today: date) -> Outcome:
    txn = _pick(p.target, user, today, verb="remover")
    if isinstance(txn, ReplyPlan):
        return Outcome(p.intent, txn.reply, txn.code)
    try:
        txn = soft_delete_transaction(actor=user, transaction_id=txn.pk, origin=idempotency_key)
    except PermissionDenied:
        return Outcome(p.intent, messages.NOT_FOUND, "NOT_FOUND")
    reply = messages.transaction_removed(
        is_income=txn.type == TransactionType.INCOME,
        amount=txn.amount,
        day=txn.transaction_date,
        where=_where(txn),
    )
    return Outcome(p.intent, reply, transaction=txn)


def _execute_ask(
    p: BulkDeletePlan, user: User, message: InboundMessage, idempotency_key: str, today: date
) -> Outcome:
    """Resolve the whole set now (locked); delete nothing until "sim" confirms this exact set."""
    rows = selectors.find_transactions(
        user=user, limit=MAX_BULK + 1, for_update=True, **p.target.filters
    )
    if not rows:
        return Outcome(p.intent, _not_found(p.target, today), "NOT_FOUND")
    if len(rows) > MAX_BULK:
        return Outcome(p.intent, messages.bulk_too_many(limit=MAX_BULK), "BULK_TOO_MANY")
    summary = selectors.summarize_own_transactions(user=user, transaction_ids=[t.pk for t in rows])
    pending.ask(
        user,
        message=message,
        action=pending.PendingAction.Action.DELETE_MANY,
        transactions=rows,
        total=summary.total,
        criteria=p.target.criteria(),
        origin=idempotency_key,
    )
    reply = messages.confirm_bulk_delete(
        type_=p.target.filters.get("type"),
        count=summary.count,
        total=summary.total,
        **_details(p.target, today),
    )
    return Outcome(p.intent, reply, "CONFIRMATION_REQUIRED")


def _execute_answer(
    p: AnswerPlan, user: User, message: InboundMessage, idempotency_key: str
) -> Outcome:
    """Apply or cancel the pending action. It is consumed whatever the answer or outcome.

    Valid only as the sender's very next message after the question; on "sim" every presented
    transaction must still be the sender's, active, unchanged (fingerprint) and match the
    approved criteria — otherwise nothing is deleted.
    """
    item = pending.take(user)
    if item is None or not pending.answers(item, message):
        return Outcome(p.intent, messages.NO_PENDING_ACTION, "NO_PENDING_ACTION")
    if pending.is_expired(item):
        return Outcome(p.intent, messages.CONFIRMATION_EXPIRED, "CONFIRMATION_EXPIRED")
    if p.decision != pending.CONFIRM:
        return Outcome(p.intent, messages.CONFIRMATION_CANCELLED, "CONFIRMATION_CANCELLED")
    stale = Outcome(p.intent, messages.CONFIRMATION_STALE, "CONFIRMATION_STALE")
    ids = item.transaction_ids
    target = _Target.from_criteria(item.criteria)
    if target is None:
        return stale
    rows = selectors.find_transactions(
        user=user, transaction_ids=ids, limit=len(ids) + 1, for_update=True, **target.filters
    )
    if len(rows) != len(ids) or pending.fingerprint(rows) != item.fingerprint:
        return stale
    summary = selectors.summarize_own_transactions(user=user, transaction_ids=ids)
    if (summary.count, summary.total) != (item.count, item.total):
        return stale
    try:
        deleted = soft_delete_transactions(actor=user, transaction_ids=ids, origin=idempotency_key)
    except (PermissionDenied, ValidationError):
        return stale
    types = {txn.type for txn in deleted}
    reply = messages.bulk_deleted(
        type_=types.pop() if len(types) == 1 else None, count=len(deleted), total=summary.total
    )
    return Outcome(p.intent, reply)


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
        reply = messages.monthly_trend(trend, today=today, capped=p.months_capped, who=who)
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
