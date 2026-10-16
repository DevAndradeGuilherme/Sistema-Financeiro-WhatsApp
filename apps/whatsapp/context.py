"""Short-lived, per-user conversation context (PostgreSQL; no cache server)."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.accounts.models import User
from apps.transactions.models import Transaction, TransactionChange

from .models import ConversationContext, InboundMessage
from .services import idempotency_key

# Messages whose committed effect a context-only correction ("errado, foram 80") may edit.
CORRECTABLE_INTENTS = ("CREATE_TRANSACTION", "UPDATE_TRANSACTION")
_ORIGIN_MAX = TransactionChange._meta.get_field("origin").max_length


def ttl() -> timedelta:
    return timedelta(minutes=settings.CONVERSATION_CONTEXT_TTL_MINUTES)


def get_previous_query(user: User) -> dict | None:
    """The sender's last validated query, if it has not expired."""
    context = ConversationContext.objects.filter(user=user, expires_at__gt=timezone.now()).first()
    return context.last_query if context else None


def remember_query(user: User, query: dict) -> None:
    """Replace the sender's context (call inside the message's atomic block)."""
    ConversationContext.objects.update_or_create(
        user=user,
        defaults={"last_query": query, "expires_at": timezone.now() + ttl()},
    )


def previous_message(message: InboundMessage) -> InboundMessage | None:
    """The sender's message reserved right before ``message`` (any status), or None."""
    return (
        InboundMessage.objects.filter(user_id=message.user_id, pk__lt=message.pk)
        .order_by("-pk")
        .first()
    )


def recent_mutation(message: InboundMessage) -> bool:
    """Some earlier message of the sender committed a CREATE/UPDATE within the context TTL —
    right before ``message`` (valid correction context) **or** with other messages in between
    (interrupted context: help, a query, a failure, invalid media, anything).

    Looser than ``correction_target`` on purpose: it only decides that an elliptic message
    ("80", "ok, 80") must not become a new transaction. An interruption means "we no longer
    know which registration to correct", never "forget it looked like a correction".
    """
    return (
        _mutations(message)
        .filter(processed_at__gt=timezone.now() - ttl(), pk__lt=message.pk)
        .exists()
    )


def _mutations(message: InboundMessage):
    return InboundMessage.objects.filter(
        user_id=message.user_id,
        status=InboundMessage.Status.PROCESSED,
        intent__in=CORRECTABLE_INTENTS,
        transaction__isnull=False,
    )


def _previous_mutation(message: InboundMessage) -> InboundMessage | None:
    previous = previous_message(message)
    if (
        previous is None
        or previous.status != InboundMessage.Status.PROCESSED
        or previous.intent not in CORRECTABLE_INTENTS
        or previous.transaction_id is None
        or previous.processed_at is None
        or previous.processed_at <= timezone.now() - ttl()
    ):
        return None
    return previous


def correction_target(message: InboundMessage) -> Transaction | None:
    """The transaction a context-only correction ("errado, foram 80") can safely refer to.

    Tied to the conversation, never to the model nor to "the latest change": the sender's
    **immediately previous** message must have committed a CREATE/UPDATE (PROCESSED, linked
    transaction), within the context TTL, and that transaction's latest change must still be
    the one made by that message (not undone, not edited since), active and owned by the
    sender. Anything in between (help, a query, a failed or invalid message, another request)
    breaks the link. Anything else → None: the caller asks instead of guessing.
    Call under the sender's lock (``pipeline.commit_plan``).
    """
    previous = _previous_mutation(message)
    if previous is None:
        return None
    change = (
        TransactionChange.objects.filter(transaction_id=previous.transaction_id)
        .select_related("transaction")
        .order_by("-created_at", "-id")
        .first()
    )
    if (
        change is None
        or change.action not in TransactionChange.REVERSIBLE
        or change.origin != idempotency_key(previous)[:_ORIGIN_MAX]
        or TransactionChange.objects.filter(reverts=change).exists()
    ):
        return None
    txn = change.transaction
    if txn.is_deleted or txn.user_id != message.user_id:
        return None
    return txn
