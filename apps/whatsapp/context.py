"""Short-lived, per-user conversation context (PostgreSQL; no cache server)."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.accounts.models import User

from .models import ConversationContext


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
