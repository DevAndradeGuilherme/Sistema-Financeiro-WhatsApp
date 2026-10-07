from datetime import UTC, datetime

import pytest

from apps.whatsapp.models import MessageType
from apps.whatsapp.parsers import (
    InvalidPayload,
    normalize_event_name,
    parse_evolution_event,
)

from . import payloads


def test_text_upsert_is_normalized():
    event = parse_evolution_event(payloads.text("  10 reais de gasolina "))
    msg = event.message
    assert event.event == "MESSAGES_UPSERT"
    assert msg.provider == "EVOLUTION"
    assert msg.instance == "test-instance"
    assert msg.provider_message_id == "3EB0A1B2C3D4E5F6"
    assert msg.remote_jid == "5531999990001@s.whatsapp.net"
    assert msg.sender_phone == "+5531999990001"
    assert msg.message_type == MessageType.TEXT
    assert msg.text == "  10 reais de gasolina "
    assert msg.timestamp == datetime.fromtimestamp(1791331200, tz=UTC)
    assert msg.from_me is False
    assert msg.is_group is False
    assert msg.metadata == {"provider_message_type": "conversation"}
    assert not hasattr(msg, "apikey")


def test_extended_text_message_is_text():
    payload = payloads.upsert(
        message_type="extendedTextMessage",
        message={"extendedTextMessage": {"text": "recebi 500 dia 2"}},
    )
    msg = parse_evolution_event(payload).message
    assert msg.message_type == MessageType.TEXT
    assert msg.text == "recebi 500 dia 2"


def test_sender_phone_uses_shared_normalizer_adding_ninth_digit():
    msg = parse_evolution_event(payloads.text(remote_jid="553199990001@s.whatsapp.net")).message
    assert msg.sender_phone == "+5531999990001"


def test_device_suffix_is_removed():
    msg = parse_evolution_event(payloads.text(remote_jid="5531999990001:12@s.whatsapp.net")).message
    assert msg.sender_phone == "+5531999990001"


def test_lid_without_alternative_has_no_sender_phone():
    msg = parse_evolution_event(payloads.text(remote_jid="123456789012345@lid")).message
    assert msg.sender_phone is None


def test_lid_with_alternative_jid_resolves_phone():
    payload = payloads.text(
        remote_jid="123456789012345@lid",
        key_extra={"remoteJidAlt": "5531999990001@s.whatsapp.net"},
    )
    assert parse_evolution_event(payload).message.sender_phone == "+5531999990001"


def test_group_message_is_flagged_without_phone():
    msg = parse_evolution_event(payloads.text(remote_jid="120363000000000000@g.us")).message
    assert msg.is_group is True
    assert msg.sender_phone is None


def test_status_broadcast_is_flagged():
    msg = parse_evolution_event(payloads.text(remote_jid="status@broadcast")).message
    assert msg.is_broadcast is True
    assert msg.sender_phone is None


def test_from_me_is_flagged():
    assert parse_evolution_event(payloads.text(from_me=True)).message.from_me is True


@pytest.mark.parametrize(
    "payload, expected",
    [
        (payloads.audio(), MessageType.AUDIO),
        (payloads.image(), MessageType.IMAGE),
        (payloads.sticker(), MessageType.UNSUPPORTED),
    ],
)
def test_media_types_are_recognized_without_text(payload, expected):
    msg = parse_evolution_event(payload).message
    assert msg.message_type == expected
    assert msg.text is None


def test_type_detected_from_content_when_message_type_missing():
    payload = payloads.audio()
    del payload["data"]["messageType"]
    assert parse_evolution_event(payload).message.message_type == MessageType.AUDIO


def test_unknown_event_has_no_message():
    event = parse_evolution_event(payloads.other_event("connection.update"))
    assert event.event == "CONNECTION_UPDATE"
    assert event.message is None


def test_event_name_normalization():
    assert normalize_event_name("messages.upsert") == "MESSAGES_UPSERT"
    assert normalize_event_name("MESSAGES_UPSERT") == "MESSAGES_UPSERT"
    assert normalize_event_name("messages-upsert") == "MESSAGES_UPSERT"


def _without(path):
    payload = payloads.text()
    target = payload
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "text",
        {},
        {"event": "messages.upsert"},
        _without(["data"]),
        _without(["data", "key"]),
        _without(["data", "key", "id"]),
        _without(["data", "key", "remoteJid"]),
        payloads.text(message_id="   "),
        payloads.text(message_id="x" * 200),
    ],
)
def test_malformed_payloads_raise_invalid_payload(payload):
    with pytest.raises(InvalidPayload):
        parse_evolution_event(payload)


def test_invalid_timestamp_becomes_none():
    payload = payloads.text()
    payload["data"]["messageTimestamp"] = "not-a-number"
    assert parse_evolution_event(payload).message.timestamp is None


def test_repr_hides_content_and_phone():
    msg = parse_evolution_event(payloads.text("segredo financeiro")).message
    assert "segredo" not in repr(msg)
    assert "5531" not in repr(msg)


def test_voice_note_metadata_is_parsed():
    payload = payloads.upsert(
        message_type="audioMessage",
        message={
            "audioMessage": {
                "mimetype": "audio/ogg; codecs=opus",
                "seconds": 7,
                "ptt": True,
                "fileLength": {"low": 12345, "high": 0, "unsigned": True},
                "url": "https://mmg.whatsapp.net/x",
            }
        },
    )
    msg = parse_evolution_event(payload).message
    assert msg.message_type == MessageType.AUDIO
    assert msg.metadata == {
        "provider_message_type": "audioMessage",
        "declared_seconds": "7",
        "declared_bytes": "12345",
        "voice_note": "true",
    }
    assert "mmg.whatsapp.net" not in str(msg.metadata)  # media URLs never leave the parser


def test_audio_file_without_ptt_is_audio_and_document_is_not():
    audio_file = payloads.upsert(
        message_type="audioMessage",
        message={"audioMessage": {"mimetype": "audio/mpeg", "fileLength": "999"}},
    )
    msg = parse_evolution_event(audio_file).message
    assert (msg.message_type, msg.metadata["voice_note"], msg.metadata["declared_bytes"]) == (
        MessageType.AUDIO,
        "false",
        "999",
    )
    document = payloads.upsert(
        message_type="documentMessage",
        message={"documentMessage": {"mimetype": "audio/ogg", "fileName": "a.ogg"}},
    )
    assert parse_evolution_event(document).message.message_type == MessageType.UNSUPPORTED


@pytest.mark.parametrize("bad", [-5, "abc", True, {"low": "x"}, None])
def test_invalid_declared_audio_values_are_dropped(bad):
    payload = payloads.upsert(
        message_type="audioMessage", message={"audioMessage": {"seconds": bad, "fileLength": bad}}
    )
    metadata = parse_evolution_event(payload).message.metadata
    assert "declared_seconds" not in metadata and "declared_bytes" not in metadata
