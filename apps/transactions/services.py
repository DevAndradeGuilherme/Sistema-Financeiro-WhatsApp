"""State-changing operations for transactions. Every function receives the actor explicitly.

Every mutation is recorded in ``TransactionChange`` in the same database transaction.
Mutations that depend on "the user's latest state" (update, undo) serialize per user with a
row lock on the user (``FOR NO KEY UPDATE``), so concurrent messages cannot interleave.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.categories.models import Category, Subcategory
from apps.families.models import Family
from apps.families.selectors import get_membership
from common.dates import today_local
from common.money import TWO_PLACES, InvalidAmount, has_at_most_two_places, to_decimal

from .models import Transaction, TransactionChange, TransactionSource, TransactionType, snapshot
from .policies import can_modify_transaction

Action = TransactionChange.Action


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()  # "field not being changed" (None may be a valid new value, e.g. subcategory)


def create_transaction(
    *,
    user: User,
    type: str,
    amount: Decimal | int | str,
    category: Category,
    subcategory: Subcategory | None = None,
    description: str = "",
    transaction_date: date | None = None,
    source: str,
    family: Family | None = None,
    idempotency_key: str | None = None,
) -> Transaction:
    """Validate and persist an expense/income owned by ``user``.

    - ``amount``: Decimal, int or dot-separated string; float is rejected.
    - ``transaction_date``: defaults to today in the business timezone; future dates rejected.
    - ``family``: derived from the user's membership; passing a different family is rejected.
    - ``idempotency_key``: if a transaction with this key already exists for the same user,
      it is returned unchanged (no duplicate is created).
    """
    if idempotency_key is not None:
        existing = _get_by_idempotency_key(user=user, key=idempotency_key)
        if existing is not None:
            return existing

    errors: dict[str, str] = {}

    if not user.pk or not user.is_active:
        errors["user"] = "Usuário inativo ou inexistente."

    if type not in TransactionType.values:
        errors["type"] = "Tipo de transação inválido."

    if source not in TransactionSource.values:
        errors["source"] = "Origem inválida."

    value = _validate_amount(amount, errors)

    if not category.is_active:
        errors["category"] = "Categoria inativa."
    elif type in TransactionType.values and not category.accepts(type):
        errors["category"] = "Categoria incompatível com o tipo da transação."

    if subcategory is not None:
        _validate_subcategory(category, subcategory, errors)

    if transaction_date is None:
        transaction_date = today_local()
    else:
        _validate_date(transaction_date, errors)

    membership = get_membership(user=user) if user.pk else None
    user_family = membership.family if membership else None
    if family is not None and (user_family is None or family.pk != user_family.pk):
        errors["family"] = "Usuário não pertence à família informada."

    if errors:
        raise ValidationError(errors)

    instance = Transaction(
        user=user,
        family=user_family,
        type=type,
        amount=value,
        category=category,
        subcategory=subcategory,
        description=_clean_description(description),
        transaction_date=transaction_date,
        source=source,
        idempotency_key=idempotency_key,
    )
    instance.full_clean(validate_unique=False)

    try:
        with transaction.atomic():
            instance.save(force_insert=True)
            _record(instance, user, Action.CREATE, after=snapshot(instance), origin=idempotency_key)
    except IntegrityError:
        # Concurrent insert with the same idempotency key.
        if idempotency_key is not None:
            existing = _get_by_idempotency_key(user=user, key=idempotency_key)
            if existing is not None:
                return existing
        raise
    return instance


@dataclass(frozen=True)
class UpdateResult:
    transaction: Transaction
    before: dict
    change: TransactionChange | None  # None when nothing actually changed


def update_transaction(
    *,
    user: User,
    transaction_id,
    amount: Decimal | int | str | _Unset = UNSET,
    description: str | _Unset = UNSET,
    category: Category | _Unset = UNSET,
    subcategory: Subcategory | None | _Unset = UNSET,
    transaction_date: date | _Unset = UNSET,
    origin: str = "",
) -> UpdateResult:
    """Edit financial fields of the user's own active transaction.

    Owner, family, type, source and technical ids are never editable. Every rule of
    ``create_transaction`` is re-applied to the changed fields. Changing the category without
    a subcategory keeps the current subcategory only if it belongs to the new category.
    """
    with transaction.atomic():
        _lock_user(user)
        txn = (
            Transaction.objects.select_for_update(of=("self",))
            .select_related("category", "subcategory")
            .filter(pk=transaction_id)
            .first()
        )
        if txn is None or not can_modify_transaction(actor=user, transaction=txn):
            raise PermissionDenied("Transação não encontrada.")
        if txn.is_deleted:
            raise ValidationError({"transaction": "Transação excluída não pode ser alterada."})

        errors: dict[str, str] = {}
        before = snapshot(txn)

        if amount is not UNSET:
            value = _validate_amount(amount, errors)
            if value is not None:
                txn.amount = value.quantize(TWO_PLACES)

        if category is not UNSET:
            if not category.is_active:
                errors["category"] = "Categoria inativa."
            elif not category.accepts(txn.type):
                errors["category"] = "Categoria incompatível com o tipo da transação."
            else:
                txn.category = category
                if (
                    subcategory is UNSET
                    and txn.subcategory_id is not None
                    and txn.subcategory.category_id != category.pk
                ):
                    txn.subcategory = None

        if subcategory is not UNSET:
            if subcategory is None:
                txn.subcategory = None
            elif _validate_subcategory(txn.category, subcategory, errors):
                txn.subcategory = subcategory

        if transaction_date is not UNSET and _validate_date(transaction_date, errors):
            txn.transaction_date = transaction_date

        if description is not UNSET:
            txn.description = _clean_description(description)

        if errors:
            raise ValidationError(errors)

        after = snapshot(txn)
        if after == before:
            return UpdateResult(transaction=txn, before=before, change=None)
        txn.full_clean(validate_unique=False)
        txn.save(
            update_fields=[
                "amount",
                "description",
                "category",
                "subcategory",
                "transaction_date",
                "updated_at",
            ]
        )
        change = _record(txn, user, Action.UPDATE, before=before, after=after, origin=origin)
    return UpdateResult(transaction=txn, before=before, change=change)


@dataclass(frozen=True)
class UndoResult:
    undo: TransactionChange
    reverted: TransactionChange
    transaction: Transaction


def undo_last_change(*, user: User, origin: str = "") -> UndoResult | None:
    """Undo the user's most recent reversible change (CREATE or UPDATE) not yet undone.

    - UNDO of CREATE soft-deletes the transaction; UNDO of UPDATE restores ``before``.
    - Each call undoes one step (stack); UNDO itself is not undoable; no redo.
    - Changes of transactions already deleted are skipped. Returns None if nothing to undo.
    """
    with transaction.atomic():
        _lock_user(user)
        target = (
            TransactionChange.objects.filter(
                user=user,
                transaction__user=user,
                action__in=TransactionChange.REVERSIBLE,
                reverted_by__isnull=True,
                transaction__deleted_at__isnull=True,
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if target is None:
            return None
        txn = Transaction.objects.select_for_update().get(pk=target.transaction_id)
        if not can_modify_transaction(actor=user, transaction=txn):
            raise PermissionDenied("Transação não encontrada.")

        before = snapshot(txn)
        if target.action == Action.CREATE:
            txn.mark_deleted()
            txn.save(update_fields=["deleted_at", "updated_at"])
            after = None
        else:
            _restore(txn, target.before)
            txn.save(
                update_fields=[
                    "amount",
                    "description",
                    "category",
                    "subcategory",
                    "transaction_date",
                    "updated_at",
                ]
            )
            after = snapshot(txn)
        undo = _record(
            txn, user, Action.UNDO, before=before, after=after, origin=origin, reverts=target
        )
    return UndoResult(undo=undo, reverted=target, transaction=txn)


def soft_delete_transaction(*, actor: User, transaction_id) -> Transaction:
    """Soft delete a transaction. Only the owner may do it (Family ADMIN cannot)."""
    with transaction.atomic():
        instance = (
            Transaction.objects.select_for_update().active().filter(pk=transaction_id).first()
        )
        if instance is None or not can_modify_transaction(actor=actor, transaction=instance):
            # Same error whether it does not exist or belongs to someone else.
            raise PermissionDenied("Transação não encontrada.")
        before = snapshot(instance)
        instance.mark_deleted()
        instance.save(update_fields=["deleted_at", "updated_at"])
        _record(instance, actor, Action.DELETE, before=before)
    return instance


# --- Helpers -------------------------------------------------------------------------------


def _validate_amount(amount, errors: dict) -> Decimal | None:
    try:
        value = to_decimal(amount)
    except InvalidAmount as exc:
        errors["amount"] = str(exc)
        return None
    if value <= 0:
        errors["amount"] = "O valor deve ser maior que zero."
    elif not has_at_most_two_places(value):
        errors["amount"] = "O valor deve ter no máximo 2 casas decimais."
    elif value > settings.TRANSACTION_MAX_AMOUNT:
        errors["amount"] = "O valor excede o limite permitido por transação."
    else:
        return value
    return None


def _validate_subcategory(category: Category, subcategory: Subcategory, errors: dict) -> bool:
    if subcategory.category_id != category.pk:
        errors["subcategory"] = "Subcategoria não pertence à categoria informada."
    elif not subcategory.is_active:
        errors["subcategory"] = "Subcategoria inativa."
    else:
        return True
    return False


def _validate_date(value, errors: dict) -> bool:
    if isinstance(value, datetime) or not isinstance(value, date):
        errors["transaction_date"] = "Data da transação deve ser uma data (sem horário)."
    elif value > today_local():
        errors["transaction_date"] = "Data da transação não pode estar no futuro."
    else:
        return True
    return False


def _clean_description(description: str | None) -> str:
    return " ".join((description or "").split())[:255]


def _restore(txn: Transaction, data: dict) -> None:
    txn.amount = Decimal(data["amount"])
    txn.description = data["description"]
    txn.category_id = data["category_id"]
    txn.subcategory_id = data["subcategory_id"]
    txn.transaction_date = date.fromisoformat(data["transaction_date"])


def _lock_user(user: User) -> None:
    # FOR NO KEY UPDATE: serializes this user's mutations without blocking FK inserts.
    User.objects.select_for_update(no_key=True).filter(pk=user.pk).first()


def _record(
    txn: Transaction,
    user: User,
    action: str,
    *,
    before: dict | None = None,
    after: dict | None = None,
    origin: str | None = "",
    reverts: TransactionChange | None = None,
) -> TransactionChange:
    return TransactionChange.objects.create(
        transaction=txn,
        user=user,
        action=action,
        before=before,
        after=after,
        origin=(origin or "")[:200],
        reverts=reverts,
    )


def _get_by_idempotency_key(*, user: User, key: str) -> Transaction | None:
    existing = Transaction.objects.filter(idempotency_key=key).first()
    if existing is None:
        return None
    if existing.user_id != user.pk:
        raise ValidationError({"idempotency_key": "Chave de idempotência já utilizada."})
    return existing
