"""Receipt images: download → validate/normalize in memory → ONE vision call → Django
validation → existing financial domain. The image is never retained.

An image is only another input transport. After extraction, the plan goes through the same
atomic ``pipeline.commit_plan`` used by text and audio (``create_transaction``, idempotency
key, PROCESSED), with ``source=IMAGE``; the attachment metadata row is written in that same
transaction.

Receipt images are processed ephemerally in memory and are not retained: no Storage, no
temporary file, no bytes/base64/URL in the database or logs. Only technical metadata
(``ReceiptAttachment``: type, size, dimensions, SHA-256 of the normalized bytes, document type,
status) and the resulting ``Transaction`` persist.

Order (no database transaction is held during HTTP calls):

1. Status must be RECEIVED (reserved by the webhook; duplicates never get here).
2. Declared size above the limit → answered rejection, no download.
3. Download through Evolution by message id only (size-capped, base64 validated).
4. Validate and re-encode with Pillow (format from bytes, pixel limit, EXIF/GPS removed);
   the original bytes are dropped right away.
5. One vision call with the normalized bytes. Failure → attachment FAILED + message FAILED.
6. ``plan_receipt`` (Django rules) → commit (transaction + attachment PROCESSED + message
   PROCESSED, atomically) → local reply.
"""

import logging
import time
from datetime import date

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from apps.ai.service import AIError
from apps.ai.vision import extract_receipt
from common.dates import today_local
from common.images import InvalidImage, NormalizedImage, normalize_image

from . import actions, messages
from .audio import reject_media
from .client import EvolutionClient, EvolutionError, MediaTooLargeError
from .models import InboundMessage, MessageType, ReceiptAttachment
from .parsers import NormalizedMessage
from .pipeline import REPLY_SEND_FAILED, commit_plan, send_reply
from .services import mark_failed

logger = logging.getLogger(__name__)

Status = InboundMessage.Status


class _AttachmentNotSaved(Exception):
    """The metadata row could not be written inside the commit (everything rolls back)."""


def handle_image_message(record: InboundMessage, message: NormalizedMessage) -> None:
    if (
        record.status != Status.RECEIVED
        or record.message_type != MessageType.IMAGE
        or record.user_id is None
    ):
        return

    declared = message.metadata.get("declared_bytes")
    if declared and declared.isdigit() and int(declared) > settings.IMAGE_MAX_BYTES:
        return reject_media(record, "IMAGE_TOO_LARGE", messages.IMAGE_TOO_LARGE)

    started = time.monotonic()
    try:
        media = EvolutionClient().download_image(
            record.provider_message_id, max_bytes=settings.IMAGE_MAX_BYTES
        )
    except MediaTooLargeError:
        return reject_media(record, "IMAGE_TOO_LARGE", messages.IMAGE_TOO_LARGE)
    except (EvolutionError, ValueError) as exc:
        logger.warning(
            "image.download_failed provider_message_id=%s error=%s",
            record.provider_message_id,
            type(exc).__name__,
        )
        mark_failed(record, "IMAGE_DOWNLOAD_FAILED")
        send_reply(record, messages.IMAGE_DOWNLOAD_FAILED)
        return
    download_ms = int((time.monotonic() - started) * 1000)

    try:
        image = normalize_image(
            media.data, max_bytes=settings.IMAGE_MAX_BYTES, max_pixels=settings.IMAGE_MAX_PIXELS
        )
    except InvalidImage as exc:
        reply = (
            messages.IMAGE_TOO_LARGE if exc.code == "IMAGE_TOO_LARGE" else messages.IMAGE_INVALID
        )
        return reject_media(record, exc.code, reply)
    finally:
        del media  # original bytes (with possible EXIF/GPS) are dropped right away
    logger.info(
        "image.validated provider_message_id=%s bytes=%d format=%s size=%dx%d download_ms=%d",
        record.provider_message_id,
        len(image.data),
        image.extension,
        image.width,
        image.height,
        download_ms,
    )
    metadata = _metadata(image)

    context = actions.build_context(today=today_local(), timezone=settings.TIME_ZONE)
    try:
        result = extract_receipt(
            image.data,
            content_type=image.content_type,
            context=context,
            log_ref=record.provider_message_id,
        )
    except AIError as exc:
        code = f"VISION_{exc.code}"[:50]
        _fail(record, metadata, message_code=code, attachment_code=code)
        send_reply(record, messages.TECHNICAL_ERROR)
        return
    finally:
        del image  # the normalized bytes are not kept either

    extraction = result.extraction
    plan = actions.plan_receipt(extraction, message_date=_message_date(record))

    def save_attachment(locked: InboundMessage, outcome: actions.Outcome) -> None:
        try:
            ReceiptAttachment.objects.create(
                inbound_message=locked,
                status=ReceiptAttachment.Status.PROCESSED,
                document_type=extraction.document_type,
                transaction=outcome.transaction,
                error_code=outcome.code,
                **metadata,
            )
        except IntegrityError as exc:
            raise _AttachmentNotSaved from exc

    try:
        outcome = commit_plan(
            record,
            plan,
            today_local(),
            expected_status=Status.RECEIVED,
            after_effect=save_attachment,
        )
    except _AttachmentNotSaved:
        # Rolled back: no transaction, no attachment. The message must not stay RECEIVED.
        logger.warning(
            "image.attachment_conflict provider_message_id=%s", record.provider_message_id
        )
        mark_failed(record, "IMAGE_ATTACHMENT_FAILED")
        send_reply(record, messages.TECHNICAL_ERROR)
        return
    except Exception:
        logger.exception("image.pipeline_error provider_message_id=%s", record.provider_message_id)
        _fail(
            record,
            metadata,
            message_code="IMAGE_PIPELINE_ERROR",
            attachment_code="PIPELINE_ERROR",
            document_type=extraction.document_type,
        )
        send_reply(record, messages.TECHNICAL_ERROR)
        return
    if outcome is None:
        return

    logger.info(
        "image.processed provider_message_id=%s document_type=%s outcome=%s transaction=%s",
        record.provider_message_id,
        extraction.document_type,
        outcome.code or "OK",
        "yes" if outcome.transaction else "no",
    )
    if not send_reply(record, outcome.reply) and not outcome.code:
        InboundMessage.objects.filter(pk=record.pk).update(error_code=REPLY_SEND_FAILED)


def _metadata(image: NormalizedImage) -> dict:
    """Technical metadata of the normalized image (exactly what the vision model received)."""
    return {
        "content_type": image.content_type,
        "byte_size": len(image.data),
        "sha256": image.sha256,
        "width": image.width,
        "height": image.height,
    }


def _fail(
    record: InboundMessage,
    metadata: dict,
    *,
    message_code: str,
    attachment_code: str,
    document_type: str = "",
) -> None:
    """Message FAILED plus a FAILED metadata row for diagnosis, atomically and only once.

    If the row cannot be written, the message still becomes FAILED (never stuck in RECEIVED).
    """
    try:
        with transaction.atomic():
            locked = InboundMessage.objects.select_for_update(of=("self",)).get(pk=record.pk)
            if locked.status != Status.RECEIVED:
                return
            ReceiptAttachment.objects.create(
                inbound_message=locked,
                status=ReceiptAttachment.Status.FAILED,
                error_code=attachment_code,
                document_type=document_type,
                **metadata,
            )
            mark_failed(record, message_code)
    except DatabaseError:
        logger.warning(
            "image.failure_not_recorded provider_message_id=%s", record.provider_message_id
        )
        mark_failed(record, message_code)


def _message_date(record: InboundMessage) -> date:
    """Date the message was received (business timezone), never in the future."""
    today = today_local()
    if record.provider_timestamp is None:
        return today
    return min(timezone.localtime(record.provider_timestamp).date(), today)
