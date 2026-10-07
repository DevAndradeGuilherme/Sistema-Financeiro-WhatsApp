"""Receipt images (V1.4): download → validate/normalize → private Storage → ONE vision call
→ Django validation → existing financial domain.

An image is only another input transport. After extraction, the plan goes through the same
atomic ``pipeline.commit_plan`` used by text and audio (``create_transaction``, idempotency
key, PROCESSED), with ``source=IMAGE``; the attachment is linked in that same transaction.

Order (no database transaction is held during HTTP calls):

1. Status must be RECEIVED (reserved by the webhook; duplicates never get here).
2. Declared size above the limit → answered rejection, no download.
3. Download through Evolution by message id only (size-capped, base64 validated).
4. Validate and re-encode with Pillow (format from bytes, pixel limit, EXIF/GPS removed).
5. Upload to the PRIVATE bucket at an opaque path; only then the attachment row (STORED).
   Upload failure → no vision call, no transaction.
6. One vision call. Failure → attachment FAILED (file kept as evidence), message FAILED.
7. ``plan_receipt`` (Django rules) → commit (transaction + link) → local reply.
"""

import contextlib
import logging
import time
import uuid
from datetime import date

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.utils import timezone

from apps.ai.service import AIError
from apps.ai.vision import extract_receipt
from common.dates import today_local
from common.images import InvalidImage, normalize_image
from common.storage import StorageClient, StorageError

from . import actions, messages
from .audio import reject_media
from .client import EvolutionClient, EvolutionError, MediaTooLargeError
from .models import InboundMessage, MessageType, ReceiptAttachment
from .parsers import NormalizedMessage
from .pipeline import REPLY_SEND_FAILED, commit_plan, send_reply
from .services import mark_failed

logger = logging.getLogger(__name__)

Status = InboundMessage.Status


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

    attachment = _store(record, image)
    if attachment is None:
        return

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
        ReceiptAttachment.objects.filter(pk=attachment.pk).update(
            status=ReceiptAttachment.Status.FAILED, error_code=code, updated_at=timezone.now()
        )
        mark_failed(record, code)
        send_reply(record, messages.TECHNICAL_ERROR)
        return
    finally:
        del image

    extraction = result.extraction
    plan = actions.plan_receipt(extraction, message_date=_message_date(record))

    def link_attachment(locked: InboundMessage, outcome: actions.Outcome) -> None:
        ReceiptAttachment.objects.filter(pk=attachment.pk).update(
            status=ReceiptAttachment.Status.PROCESSED,
            document_type=extraction.document_type,
            transaction=outcome.transaction,
            error_code=outcome.code,
            updated_at=timezone.now(),
        )

    try:
        outcome = commit_plan(
            record,
            plan,
            today_local(),
            expected_status=Status.RECEIVED,
            after_effect=link_attachment,
        )
    except Exception:
        logger.exception("image.pipeline_error provider_message_id=%s", record.provider_message_id)
        ReceiptAttachment.objects.filter(pk=attachment.pk).update(
            status=ReceiptAttachment.Status.FAILED,
            error_code="PIPELINE_ERROR",
            updated_at=timezone.now(),
        )
        mark_failed(record, "IMAGE_PIPELINE_ERROR")
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
    reply = outcome.reply + (messages.RECEIPT_STORED_SUFFIX if outcome.transaction else "")
    if not send_reply(record, reply) and not outcome.code:
        InboundMessage.objects.filter(pk=record.pk).update(error_code=REPLY_SEND_FAILED)


def _store(record: InboundMessage, image) -> ReceiptAttachment | None:
    """Upload to the private bucket at an opaque path, then persist metadata (STORED)."""
    now = timezone.localtime()
    path = f"receipts/{now:%Y}/{now:%m}/{uuid.uuid4().hex}.{image.extension}"
    try:
        storage = StorageClient()
        storage.upload(path, image.data, image.content_type)
    except (StorageError, ImproperlyConfigured) as exc:
        code = f"STORAGE_{getattr(exc, 'code', 'CONFIG')}"[:50]
        logger.warning(
            "image.storage_failed provider_message_id=%s code=%s",
            record.provider_message_id,
            code,
        )
        mark_failed(record, code)
        send_reply(record, messages.TECHNICAL_ERROR)
        return None

    try:
        return ReceiptAttachment.objects.create(
            inbound_message=record,
            storage_bucket=storage.config.bucket,
            storage_path=path,
            content_type=image.content_type,
            byte_size=len(image.data),
            sha256=image.sha256,
            width=image.width,
            height=image.height,
        )
    except IntegrityError:
        # Only one flow can reach here per message (reservation); be safe anyway.
        logger.warning(
            "image.attachment_conflict provider_message_id=%s", record.provider_message_id
        )
        with contextlib.suppress(StorageError):
            storage.delete(path)
        return None


def _message_date(record: InboundMessage) -> date:
    """Date the message was received (business timezone), never in the future."""
    today = today_local()
    if record.provider_timestamp is None:
        return today
    return min(timezone.localtime(record.provider_timestamp).date(), today)
