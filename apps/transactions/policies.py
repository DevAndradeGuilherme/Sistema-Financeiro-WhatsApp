from apps.accounts.models import User

from .models import Transaction


def can_modify_transaction(*, actor: User, transaction: Transaction) -> bool:
    """Only the owner may edit, undo or delete. Family ADMIN has read-only access."""
    return actor.pk is not None and actor.is_active and transaction.user_id == actor.pk
