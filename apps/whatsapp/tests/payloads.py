"""Evolution API v2.3.7 webhook payloads (shape taken from the running version; data anonymized)."""

import copy

INSTANCE = "test-instance"


def upsert(
    *,
    message_id: str = "3EB0A1B2C3D4E5F6",
    remote_jid: str = "5531999990001@s.whatsapp.net",
    from_me: bool = False,
    message_type: str = "conversation",
    message: dict | None = None,
    instance: str = INSTANCE,
    key_extra: dict | None = None,
) -> dict:
    key = {"remoteJid": remote_jid, "fromMe": from_me, "id": message_id}
    key.update(key_extra or {})
    return {
        "event": "messages.upsert",
        "instance": instance,
        "data": {
            "key": key,
            "pushName": "Fulano",
            "status": "DELIVERY_ACK",
            "message": message if message is not None else {"conversation": "10 reais de gasolina"},
            "messageType": message_type,
            "messageTimestamp": 1791331200,
            "instanceId": "00000000-0000-0000-0000-000000000000",
            "source": "android",
        },
        "destination": "http://testserver/api/v1/webhooks/evolution/",
        "date_time": "2026-10-06T21:00:00.000Z",
        "sender": "5531900000000@s.whatsapp.net",
        "server_url": "http://evolution.test",
        "apikey": "instance-token-that-must-never-be-stored",
    }


def text(body: str = "10 reais de gasolina", **kwargs) -> dict:
    return upsert(message={"conversation": body}, message_type="conversation", **kwargs)


def audio(**kwargs) -> dict:
    return upsert(
        message_type="audioMessage",
        message={"audioMessage": {"mimetype": "audio/ogg; codecs=opus", "seconds": 4, "ptt": True}},
        **kwargs,
    )


def image(**kwargs) -> dict:
    return upsert(
        message_type="imageMessage",
        message={"imageMessage": {"mimetype": "image/jpeg", "caption": "recibo"}},
        **kwargs,
    )


def sticker(**kwargs) -> dict:
    return upsert(
        message_type="stickerMessage",
        message={"stickerMessage": {"mimetype": "image/webp"}},
        **kwargs,
    )


def other_event(name: str = "connection.update") -> dict:
    return {"event": name, "instance": INSTANCE, "data": {"state": "open"}}


def clone(payload: dict) -> dict:
    return copy.deepcopy(payload)
