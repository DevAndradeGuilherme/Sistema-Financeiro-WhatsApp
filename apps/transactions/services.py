"""State-changing operations for transactions. Every function receives the actor explicitly."""

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
from common.money import InvalidAmount, has_at_most_two_places, to_decimal

from .models import Transaction, TransactionSource, TransactionType
from .policies import can_modify_transaction


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

    try:
        value = to_decimal(amount)
    except InvalidAmount as exc:
        errors["amount"] = str(exc)
    else:
        if value <= 0:
            errors["amount"] = "O valor deve ser maior que zero."
        elif not has_at_most_two_places(value):
            errors["amount"] = "O valor deve ter no máximo 2 casas decimais."
        elif value > settings.TRANSACTION_MAX_AMOUNT:
            errors["amount"] = "O valor excede o limite permitido por transação."

    if not category.is_active:
        errors["category"] = "Categoria inativa."
    elif type in TransactionType.values and not category.accepts(type):
        errors["category"] = "Categoria incompatível com o tipo da transação."

    if subcategory is not None:
        if subcategory.category_id != category.pk:
            errors["subcategory"] = "Subcategoria não pertence à categoria informada."
        elif not subcategory.is_active:
            errors["subcategory"] = "Subcategoria inativa."

    if transaction_date is None:
        transaction_date = today_local()
    elif isinstance(transaction_date, datetime) or not isinstance(transaction_date, date):
        errors["transaction_date"] = "Data da transação deve ser uma data (sem horário)."
    elif transaction_date > today_local():
        errors["transaction_date"] = "Data da transação não pode estar no futuro."

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
        description=" ".join((description or "").split()),
        transaction_date=transaction_date,
        source=source,
        idempotency_key=idempotency_key,
    )
    instance.full_clean(validate_unique=False)

    try:
        with transaction.atomic():
            instance.save(force_insert=True)
    except IntegrityError:
        # Concurrent insert with the same idempotency key.
        if idempotency_key is not None:
            existing = _get_by_idempotency_key(user=user, key=idempotency_key)
            if existing is not None:
                return existing
        raise
    return instance


def soft_delete_transaction(*, actor: User, transaction_id) -> Transaction:
    """Soft delete a transaction. Only the owner may do it (Family ADMIN cannot)."""
    with transaction.atomic():
        instance = (
            Transaction.objects.select_for_update().active().filter(pk=transaction_id).first()
        )
        if instance is None or not can_modify_transaction(actor=actor, transaction=instance):
            # Same error whether it does not exist or belongs to someone else.
            raise PermissionDenied("Transação não encontrada.")
        instance.mark_deleted()
        instance.save(update_fields=["deleted_at", "updated_at"])
    return instance


def _get_by_idempotency_key(*, user: User, key: str) -> Transaction | None:
    existing = Transaction.objects.filter(idempotency_key=key).first()
    if existing is None:
        return None
    if existing.user_id != user.pk:
        raise ValidationError({"idempotency_key": "Chave de idempotência já utilizada."})
    return existing
