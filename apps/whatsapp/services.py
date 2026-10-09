"""Inbound message service: idempotent reservation, filtering and sender identification.

Flow (docs/WHATSAPP.md): reserve the provider id (unique row) → classify → READY or IGNORED
(audio from a known user stays RECEIVED until transcribed, see ``audio.py``).
A duplicate delivery stops at the reservation and never runs the rest again — including the
audio download and transcription.
"""

import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User

from .models import InboundMessage, MessageType
from .parsers import NormalizedMessage

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 4096

Reason = InboundMessage.IgnoreReason
Status = InboundMessage.Status


@dataclass(frozen=True)
class InboundResult:
    record: InboundMessage
    duplicate: bool

    @property
    def is_ready(self) -> bool:
        return not self.duplicate and self.record.status == Status.READY

    @property
    def needs_transcription(self) -> bool:
        return self._pending_media(MessageType.AUDIO)

    @property
    def needs_image_processing(self) -> bool:
        return self._pending_media(MessageType.IMAGE)

    def _pending_media(self, message_type: str) -> bool:
        return (
            not self.duplicate
            and self.record.status == Status.RECEIVED
            and self.record.message_type == message_type
            and self.record.user_id is not None
        )


def receive_message(message: NormalizedMessage) -> InboundResult:
    record, created = _reserve(message)
    if not created:
        _log("inbound.duplicate", record)
        return InboundResult(record=record, duplicate=True)

    try:
        _classify(record, message)
    except Exception:
        _mark_failed(record, "CLASSIFICATION_ERROR")
        raise
    _log("inbound.received", record)
    return InboundResult(record=record, duplicate=False)


def mark_failed(record: InboundMessage, error_code: str) -> bool:
    """RECEIVED/READY → FAILED (conditional). Returns whether this call made the transition."""
    return _mark_failed(record, error_code)


def _reserve(message: NormalizedMessage) -> tuple[InboundMessage, bool]:
    """Insert the idempotency row. The unique constraint decides concurrent races."""
    try:
        with transaction.atomic():
            record = InboundMessage.objects.create(
                provider=message.provider,
                instance=message.instance,
                provider_message_id=message.provider_message_id,
                message_type=message.message_type,
                provider_timestamp=message.timestamp,
                status=Status.RECEIVED,
            )
        return record, True
    except IntegrityError:
        existing = InboundMessage.objects.get(
            provider=message.provider,
            instance=message.instance,
            provider_message_id=message.provider_message_id,
        )
        return existing, False


def _classify(record: InboundMessage, message: NormalizedMessage) -> None:
    if message.from_me:
        return _ignore(record, Reason.FROM_ME)
    if message.is_group:
        return _ignore(record, Reason.GROUP)
    if message.is_broadcast:
        return _ignore(record, Reason.BROADCAST)
    if message.sender_phone is None:
        return _ignore(record, Reason.SENDER_UNRESOLVED)

    record.sender_phone = message.sender_phone
    user = User.objects.filter(phone_number=message.sender_phone).first()
    if user is None:
        # Not created automatically; no reply (avoids spam/enumeration). See docs/WHATSAPP.md.
        return _ignore(record, Reason.UNKNOWN_SENDER)
    record.user = user
    if not user.is_active:
        return _ignore(record, Reason.INACTIVE_USER)

    if message.message_type in (MessageType.AUDIO, MessageType.IMAGE):
        # Filters above already ran (fromMe, group, broadcast, unknown/inactive sender), so no
        # download happens for messages that would be ignored. Stays RECEIVED until the media
        # handler (audio.py / image.py) finishes.
        record.save(update_fields=["sender_phone", "user", "updated_at"])
        return None
    if message.message_type != MessageType.TEXT:
        return _ignore(record, Reason.UNSUPPORTED_TYPE)

    text = (message.text or "").strip()
    if not text:
        return _ignore(record, Reason.EMPTY_TEXT)
    if len(text) > MAX_TEXT_LENGTH:
        return _ignore(record, Reason.TEXT_TOO_LONG)

    record.text = text
    record.status = Status.READY
    record.save(update_fields=["sender_phone", "user", "text", "status", "updated_at"])


def _ignore(record: InboundMessage, reason: str) -> None:
    record.status = Status.IGNORED
    record.ignore_reason = reason
    record.processed_at = timezone.now()
    record.save(
        update_fields=[
            "status",
            "ignore_reason",
            "sender_phone",
            "user",
            "processed_at",
            "updated_at",
        ]
    )


def _mark_failed(record: InboundMessage, error_code: str) -> bool:
    # Only messages without a committed effect may become FAILED.
    updated = InboundMessage.objects.filter(
        pk=record.pk, status__in=[Status.RECEIVED, Status.READY]
    ).update(
        status=Status.FAILED,
        error_code=error_code[:50],
        processed_at=timezone.now(),
        updated_at=timezone.now(),
    )
    record.refresh_from_db()
    _log("inbound.failed", record)
    return bool(updated)


def _log(event: str, record: InboundMessage) -> None:
    logger.info(
        "%s provider_message_id=%s instance=%s message_type=%s processing_status=%s reason=%s",
        event,
        record.provider_message_id,
        record.instance,
        record.message_type,
        record.status,
        record.ignore_reason or record.error_code or "-",
    )
