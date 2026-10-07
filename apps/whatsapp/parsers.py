"""Evolution API v2 webhook payload → internal representation.

Only this module knows the Evolution JSON shape (contract in docs/WHATSAPP.md). Everything
downstream (services, the future AI pipeline) uses ``NormalizedMessage``. The raw payload,
including the instance ``apikey`` Evolution sends in the body, is never kept.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from common.phone import InvalidPhoneNumber, normalize_phone

from .conf import PROVIDER
from .models import MessageType

MESSAGES_UPSERT = "MESSAGES_UPSERT"

_TEXT_TYPES = {"conversation", "extendedTextMessage"}
_AUDIO_TYPES = {"audioMessage"}
_IMAGE_TYPES = {"imageMessage"}


class InvalidPayload(ValueError):
    """Payload that will never be processable (answered with 400, not retried)."""


@dataclass(frozen=True)
class NormalizedMessage:
    provider: str
    instance: str
    provider_message_id: str
    remote_jid: str
    sender_phone: str | None
    message_type: str
    text: str | None
    timestamp: datetime | None
    from_me: bool
    is_group: bool
    is_broadcast: bool
    metadata: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # keep message content and phone out of logs/tracebacks
        return (
            f"NormalizedMessage(provider_message_id={self.provider_message_id!r}, "
            f"instance={self.instance!r}, message_type={self.message_type!r})"
        )


@dataclass(frozen=True)
class ParsedEvent:
    event: str
    instance: str
    message: NormalizedMessage | None  # None for events that are not handled


def normalize_event_name(raw: str) -> str:
    """'messages.upsert' (webhook body) and 'MESSAGES_UPSERT' (config) → 'MESSAGES_UPSERT'."""
    return raw.replace(".", "_").replace("-", "_").upper()


def parse_evolution_event(payload: Any) -> ParsedEvent:
    if not isinstance(payload, dict):
        raise InvalidPayload("payload must be a JSON object")
    raw_event = payload.get("event")
    instance = payload.get("instance")
    if not isinstance(raw_event, str) or not raw_event:
        raise InvalidPayload("missing event")
    if not isinstance(instance, str) or not instance:
        raise InvalidPayload("missing instance")

    event = normalize_event_name(raw_event)
    if event != MESSAGES_UPSERT:
        return ParsedEvent(event=event, instance=instance, message=None)

    return ParsedEvent(event=event, instance=instance, message=_parse_message(instance, payload))


def _parse_message(instance: str, payload: dict) -> NormalizedMessage:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise InvalidPayload("data must be an object")
    key = data.get("key")
    if not isinstance(key, dict):
        raise InvalidPayload("data.key must be an object")

    message_id = key.get("id")
    remote_jid = key.get("remoteJid")
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 128:
        raise InvalidPayload("invalid data.key.id")
    if not isinstance(remote_jid, str) or not remote_jid:
        raise InvalidPayload("invalid data.key.remoteJid")

    from_me = key.get("fromMe") is True
    domain = remote_jid.rsplit("@", 1)[-1] if "@" in remote_jid else ""
    is_group = domain == "g.us"
    is_broadcast = domain == "broadcast" or remote_jid.startswith("status@")

    content = data.get("message") if isinstance(data.get("message"), dict) else {}
    raw_type = data.get("messageType") if isinstance(data.get("messageType"), str) else ""
    message_type = _message_type(raw_type, content)

    return NormalizedMessage(
        provider=PROVIDER,
        instance=instance,
        provider_message_id=message_id.strip(),
        remote_jid=remote_jid,
        sender_phone=_sender_phone(remote_jid, key, is_group or is_broadcast),
        message_type=message_type,
        text=_text(content) if message_type == MessageType.TEXT else None,
        timestamp=_timestamp(data.get("messageTimestamp")),
        from_me=from_me,
        is_group=is_group,
        is_broadcast=is_broadcast,
        metadata=_metadata(raw_type, message_type, content),
    )


def _metadata(raw_type: str, message_type: str, content: dict) -> dict[str, str]:
    """Minimal technical metadata. For audio: *declared* duration/size (untrusted hints used
    only to skip obviously oversized downloads; real limits are enforced on the bytes)."""
    metadata = {"provider_message_type": raw_type[:50]} if raw_type else {}
    if message_type == MessageType.AUDIO:
        audio = content.get("audioMessage") if isinstance(content.get("audioMessage"), dict) else {}
        seconds = _non_negative_int(audio.get("seconds"))
        size = _non_negative_int(audio.get("fileLength"))
        if seconds is not None:
            metadata["declared_seconds"] = str(seconds)
        if size is not None:
            metadata["declared_bytes"] = str(size)
        metadata["voice_note"] = "true" if audio.get("ptt") is True else "false"
    if message_type == MessageType.IMAGE:
        image = content.get("imageMessage") if isinstance(content.get("imageMessage"), dict) else {}
        size = _non_negative_int(image.get("fileLength"))
        if size is not None:
            metadata["declared_bytes"] = str(size)
    return metadata


def _non_negative_int(value: Any) -> int | None:
    """Accepts 12, "12" or a protobuf Long ({"low": 12, "high": 0})."""
    if isinstance(value, dict):
        low, high = value.get("low"), value.get("high", 0)
        if isinstance(low, int) and isinstance(high, int) and not isinstance(low, bool):
            value = (high << 32) + (low & 0xFFFFFFFF)
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _message_type(raw_type: str, content: dict) -> str:
    if raw_type in _TEXT_TYPES or (not raw_type and _TEXT_TYPES & content.keys()):
        return MessageType.TEXT
    if raw_type in _AUDIO_TYPES or (not raw_type and _AUDIO_TYPES & content.keys()):
        return MessageType.AUDIO
    if raw_type in _IMAGE_TYPES or (not raw_type and _IMAGE_TYPES & content.keys()):
        return MessageType.IMAGE
    return MessageType.UNSUPPORTED


def _text(content: dict) -> str | None:
    text = content.get("conversation")
    if not isinstance(text, str):
        extended = content.get("extendedTextMessage")
        text = extended.get("text") if isinstance(extended, dict) else None
    return text if isinstance(text, str) else None


def _sender_phone(remote_jid: str, key: dict, is_group_or_broadcast: bool) -> str | None:
    """Phone of a direct chat sender, through the shared normalizer, or None.

    Evolution 2.3.x already replaces an ``@lid`` remoteJid with ``remoteJidAlt`` when known;
    the fallback below covers payloads where it did not.
    """
    if is_group_or_broadcast:
        return None
    jid = remote_jid
    if jid.endswith("@lid"):
        alt = key.get("remoteJidAlt") or key.get("senderPn")
        if not isinstance(alt, str):
            return None
        jid = alt
    if not jid.endswith("@s.whatsapp.net"):
        return None
    user_part = jid.split("@", 1)[0].split(":", 1)[0]  # drop device suffix (e.g. ":12")
    if not user_part.isdigit():
        return None
    try:
        return normalize_phone("+" + user_part)
    except InvalidPhoneNumber:
        return None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
