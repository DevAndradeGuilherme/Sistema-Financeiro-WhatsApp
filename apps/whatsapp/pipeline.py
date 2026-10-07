"""Integration point between the WhatsApp transport and the business pipeline.

``handle_ready_message`` receives READY messages (known active user, text). In V1.0 the AI
interpretation + financial services will be plugged in here. Until then it only sends an
optional technical acknowledgement in development (WHATSAPP_DEV_ECHO), never a financial reply.
"""

import logging

from django.conf import settings

from .client import EvolutionClient, EvolutionError
from .models import InboundMessage

logger = logging.getLogger(__name__)

DEV_ECHO_TEXT = "Mensagem recebida."


def handle_ready_message(record: InboundMessage) -> None:
    if not settings.WHATSAPP_DEV_ECHO:
        return
    try:
        EvolutionClient().send_text(record.sender_phone, DEV_ECHO_TEXT)
    except EvolutionError as exc:
        logger.warning(
            "inbound.dev_echo_failed provider_message_id=%s error=%s",
            record.provider_message_id,
            type(exc).__name__,
        )
