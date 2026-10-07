"""Textual pipeline for READY messages (docs/WHATSAPP.md §5).

    READY → context (active catalog) → ONE OpenAI call (no DB transaction open)
          → plan (Django validation) → atomic[lock message, effect, PROCESSED]
          → local reply → Evolution send_text (outside the transaction)

Invariants:
- the financial effect and ``status=PROCESSED`` commit together; a message that is not READY
  under the row lock is never executed again;
- a failed reply never re-runs the effect (status stays PROCESSED, ``error_code`` notes it);
- AI failures create nothing and end in FAILED with a sanitized code.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.ai.service import AIError, interpret_message
from common.dates import today_local

from . import actions, messages
from . import context as conversation
from .client import EvolutionClient, EvolutionError
from .models import InboundMessage
from .services import mark_failed

logger = logging.getLogger(__name__)

Status = InboundMessage.Status
REPLY_SEND_FAILED = "REPLY_SEND_FAILED"


def idempotency_key(record: InboundMessage) -> str:
    return f"whatsapp:{record.instance}:{record.provider_message_id}"


def handle_ready_message(record: InboundMessage) -> None:
    if record.status != Status.READY or record.user_id is None:
        return
    today = today_local()

    previous_query = conversation.get_previous_query(record.user)
    context = actions.build_context(
        today=today, timezone=settings.TIME_ZONE, previous_query=previous_query
    )
    try:
        result = interpret_message(record.text, context, log_ref=record.provider_message_id)
    except AIError as exc:
        mark_failed(record, f"AI_{exc.code}")
        _send(record, messages.TECHNICAL_ERROR)
        return

    try:
        outcome = _commit(record, result.interpretation, today, previous_query is not None)
    except Exception:
        # Unexpected failure (e.g. missing fallback category): the atomic block rolled back,
        # so there is no effect; the message becomes FAILED and the user gets a generic reply.
        logger.exception("pipeline.error provider_message_id=%s", record.provider_message_id)
        mark_failed(record, "PIPELINE_ERROR")
        _send(record, messages.TECHNICAL_ERROR)
        return
    if outcome is None:
        return

    logger.info(
        "pipeline.processed provider_message_id=%s intent=%s outcome=%s transaction=%s",
        record.provider_message_id,
        outcome.intent,
        outcome.code or "OK",
        "yes" if outcome.transaction else "no",
    )
    if not _send(record, outcome.reply) and not outcome.code:
        InboundMessage.objects.filter(pk=record.pk).update(error_code=REPLY_SEND_FAILED)


def _commit(
    record: InboundMessage, interpretation, today, has_context: bool
) -> "actions.Outcome | None":
    """Validate, then run the effect and mark PROCESSED in one transaction."""
    plan = actions.plan(interpretation, user=record.user, today=today, has_context=has_context)
    with transaction.atomic():
        locked = (
            InboundMessage.objects.select_for_update(of=("self",))
            .select_related("user")
            .get(pk=record.pk)
        )
        if locked.status != Status.READY:
            logger.info(
                "pipeline.skipped provider_message_id=%s processing_status=%s",
                locked.provider_message_id,
                locked.status,
            )
            return None
        outcome = actions.execute(
            plan, user=locked.user, idempotency_key=idempotency_key(locked), today=today
        )
        if outcome.query_context is not None:
            conversation.remember_query(locked.user, outcome.query_context)
        locked.status = Status.PROCESSED
        locked.intent = outcome.intent
        locked.error_code = outcome.code
        locked.transaction = outcome.transaction
        locked.processed_at = timezone.now()
        locked.save(
            update_fields=[
                "status",
                "intent",
                "error_code",
                "transaction",
                "processed_at",
                "updated_at",
            ]
        )
    return outcome


def _send(record: InboundMessage, text: str) -> bool:
    try:
        EvolutionClient().send_text(record.sender_phone, text)
        return True
    except (EvolutionError, ValueError) as exc:
        logger.warning(
            "pipeline.reply_failed provider_message_id=%s error=%s",
            record.provider_message_id,
            type(exc).__name__,
        )
        return False
