"""Confirmation of multi-transaction mutations (PostgreSQL row per user; no cache server).

Flow: a bulk request stores the exact candidate set (``ask``) → the next message decides.
Only a whole-message "sim"/"não" (``decision``) reaches the pending row, and only when it is
the sender's **very next** message after the one that asked (``answers``): any message in
between — whatever happened to it (model failure, invalid audio/image, help, unknown, another
request) — invalidates the question as soon as it is reserved by the webhook, so an old
question can never be confirmed by a later "sim". Other messages also delete it (``clear``).

``ask``/``take``/``clear`` run inside the message's atomic block, after the per-user lock
(``pipeline.commit_plan``), so asking, answering and any concurrent mutation are serialized
and a pending action is consumed at most once.
"""

import hashlib
import json
import re
from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import User
from apps.transactions.models import Transaction

from .hints import normalize
from .models import InboundMessage, PendingAction

TTL = timedelta(minutes=10)
CONFIRM = "CONFIRM"
DENY = "DENY"

_CONFIRM_WORDS = {
    "sim",
    "s",
    "confirmo",
    "confirma",
    "confirmar",
    "confirmado",
    "pode",
    "pode sim",
    "sim pode",
    "pode apagar",
    "pode remover",
    "pode excluir",
    "pode confirmar",
    "sim pode apagar",
    "sim pode remover",
    "sim confirmo",
    "sim pode excluir",
}
_DENY_WORDS = {
    "nao",
    "n",
    "cancela",
    "cancelar",
    "cancelado",
    "nao quero",
    "nao pode",
    "nao apaga",
    "nao apague",
    "nao precisa",
    "deixa",
    "deixa pra la",
}


def decision(text: str | None) -> str | None:
    """CONFIRM / DENY when the whole message is an explicit answer, else None."""
    words = " ".join(re.sub(r"[^a-z ]", " ", normalize(text)).split())
    if words in _CONFIRM_WORDS:
        return CONFIRM
    if words in _DENY_WORDS:
        return DENY
    return None


def fingerprint(transactions: list[Transaction]) -> str:
    """SHA-256 of every field that identifies/selects each candidate (order-independent).

    Any change to owner, state-relevant fields or the pairing id ↔ value (e.g. two amounts
    swapped, keeping the total) changes it.
    """
    rows = sorted(
        [
            str(t.pk),
            t.user_id,
            t.type,
            str(t.amount),
            t.category_id,
            t.subcategory_id,
            t.description,
            t.transaction_date.isoformat(),
        ]
        for t in transactions
    )
    canonical = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def ask(
    user: User,
    *,
    message: InboundMessage,
    action: str,
    transactions: list[Transaction],
    total,
    criteria: dict,
    origin: str,
) -> PendingAction:
    """Store (replacing any previous one) the action waiting for confirmation."""
    pending, _ = PendingAction.objects.update_or_create(
        user=user,
        defaults={
            "inbound_message": message,
            "action": action,
            "transaction_ids": [str(t.pk) for t in transactions],
            "count": len(transactions),
            "total": total,
            "criteria": criteria,
            "fingerprint": fingerprint(transactions),
            "origin": origin[:200],
            "expires_at": timezone.now() + TTL,
        },
    )
    return pending


def take(user: User) -> PendingAction | None:
    """Lock and remove the user's pending action (valid or not): it is answered once."""
    pending = PendingAction.objects.select_for_update().filter(user=user).first()
    if pending is not None:
        pending.delete()
    return pending


def answers(pending: PendingAction, message: InboundMessage) -> bool:
    """True only if ``message`` is the sender's next message after the one that asked.

    Order = reservation order of ``InboundMessage`` (pk). Every message linked to the sender
    counts, including ones that failed or were ignored after identifying the sender.
    """
    asked = pending.inbound_message_id
    if asked is None or asked >= message.pk:
        return False
    return not InboundMessage.objects.filter(
        user_id=message.user_id, pk__gt=asked, pk__lt=message.pk
    ).exists()


def is_expired(pending: PendingAction) -> bool:
    return pending.expires_at <= timezone.now()


def clear(user: User) -> None:
    """Any message that is not an answer cancels the pending question."""
    PendingAction.objects.filter(user=user).delete()
