"""Audio messages (V1.3): download → transcription → the existing textual pipeline.

Audio is only another input transport. After transcription the message becomes READY with
the transcript as its text and is handled by ``pipeline.handle_ready_message`` exactly like a
typed message (interpretation, authorization, services, context, replies). Nothing financial
happens here.

Order (no database transaction is held during HTTP calls):

1. Status must be RECEIVED (reserved by the webhook; duplicates never get here).
2. Declared duration/size above the limits → answered rejection, no download.
3. Download through Evolution by message id only, with a hard size cap.
4. Format detected from the bytes (OGG/Opus voice notes are sent as-is, no conversion).
5. One transcription call. Empty/too long transcript → answered rejection.
6. ``UPDATE ... WHERE status=RECEIVED`` → READY with the transcript (only once), then the
   textual pipeline.

Audio bytes and base64 live only in memory for the duration of the request; nothing is
written to disk or to the database except the transcript (stored like any READY text).
"""

import logging
import time

from django.conf import settings
from django.utils import timezone

from apps.ai.service import AIError
from apps.ai.transcription import transcribe_audio
from common.media import detect_audio_format

from . import messages
from .client import EvolutionClient, EvolutionError, MediaTooLargeError
from .models import InboundMessage, MessageType
from .parsers import NormalizedMessage
from .pipeline import handle_ready_message, send_reply
from .services import MAX_TEXT_LENGTH, mark_failed

logger = logging.getLogger(__name__)

Status = InboundMessage.Status


def handle_audio_message(record: InboundMessage, message: NormalizedMessage) -> None:
    if (
        record.status != Status.RECEIVED
        or record.message_type != MessageType.AUDIO
        or record.user_id is None
    ):
        return

    declared_seconds = _int(message.metadata.get("declared_seconds"))
    declared_bytes = _int(message.metadata.get("declared_bytes"))
    if declared_seconds is not None and declared_seconds > settings.AUDIO_MAX_SECONDS:
        return reject_media(record, "AUDIO_TOO_LONG", messages.AUDIO_TOO_LARGE)
    if declared_bytes is not None and declared_bytes > settings.AUDIO_MAX_BYTES:
        return reject_media(record, "AUDIO_TOO_LARGE", messages.AUDIO_TOO_LARGE)

    started = time.monotonic()
    try:
        media = EvolutionClient().download_audio(
            record.provider_message_id, max_bytes=settings.AUDIO_MAX_BYTES
        )
    except MediaTooLargeError:
        return reject_media(record, "AUDIO_TOO_LARGE", messages.AUDIO_TOO_LARGE)
    except (EvolutionError, ValueError) as exc:
        logger.warning(
            "audio.download_failed provider_message_id=%s error=%s",
            record.provider_message_id,
            type(exc).__name__,
        )
        mark_failed(record, "AUDIO_DOWNLOAD_FAILED")
        send_reply(record, messages.AUDIO_DOWNLOAD_FAILED)
        return
    download_ms = int((time.monotonic() - started) * 1000)

    audio_format = detect_audio_format(media.data)
    logger.info(
        "audio.downloaded provider_message_id=%s bytes=%d download_ms=%d format=%s "
        "declared_seconds=%s",
        record.provider_message_id,
        len(media.data),
        download_ms,
        audio_format.extension if audio_format else "unknown",
        declared_seconds if declared_seconds is not None else "-",
    )
    if audio_format is None:
        return reject_media(record, "AUDIO_UNSUPPORTED_FORMAT", messages.AUDIO_UNSUPPORTED)

    try:
        transcript = transcribe_audio(
            media.data,
            filename=f"audio.{audio_format.extension}",  # our name, never the sender's
            content_type=audio_format.content_type,
            log_ref=record.provider_message_id,
        )
    except AIError as exc:
        if exc.code in ("EMPTY_TRANSCRIPT", "EMPTY_AUDIO"):
            return reject_media(record, exc.code, messages.AUDIO_NOT_UNDERSTOOD)
        mark_failed(record, f"TRANSCRIPTION_{exc.code}"[:50])
        send_reply(record, messages.TECHNICAL_ERROR)
        return
    finally:
        del media  # drop the audio bytes as soon as possible

    if len(transcript.text) > MAX_TEXT_LENGTH:
        return reject_media(record, "TRANSCRIPT_TOO_LONG", messages.AUDIO_TOO_LARGE)

    promoted = InboundMessage.objects.filter(pk=record.pk, status=Status.RECEIVED).update(
        text=transcript.text, status=Status.READY, updated_at=timezone.now()
    )
    if not promoted:
        return  # handled elsewhere in the meantime; never run the pipeline twice
    record.refresh_from_db()
    handle_ready_message(record)


def reject_media(record: InboundMessage, code: str, reply: str) -> None:
    """Answered without any effect (same semantics as other rejections: PROCESSED + code).

    Shared by audio and image handlers; only acts while the message is still RECEIVED.
    """
    updated = InboundMessage.objects.filter(pk=record.pk, status=Status.RECEIVED).update(
        status=Status.PROCESSED,
        error_code=code[:50],
        processed_at=timezone.now(),
        updated_at=timezone.now(),
    )
    logger.info("audio.rejected provider_message_id=%s reason=%s", record.provider_message_id, code)
    if updated:
        send_reply(record, reply)


def _int(value: str | None) -> int | None:
    return int(value) if isinstance(value, str) and value.isdigit() else None
