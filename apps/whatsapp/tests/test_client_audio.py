import base64
import io
import json
from unittest import mock
from urllib.error import HTTPError

import pytest

from apps.whatsapp.client import (
    EvolutionClient,
    EvolutionHTTPError,
    EvolutionResponseError,
    EvolutionTimeoutError,
    MediaTooLargeError,
)

AUDIO = b"OggS" + b"\x01" * 500
LIMIT = 1024


class FakeResponse:
    def __init__(self, body: bytes):
        self._stream = io.BytesIO(body)

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def media_response(**overrides):
    body = {
        "mediaType": "audioMessage",
        "fileName": "../../etc/passwd.ogg",
        "size": {"fileLength": len(AUDIO)},
        "mimetype": "audio/ogg; codecs=opus",
        "base64": base64.b64encode(AUDIO).decode(),
        "buffer": None,
    }
    body.update(overrides)
    return json.dumps(body).encode()


def urlopen_returning(body: bytes, calls: list):
    def _urlopen(request, timeout=None):
        calls.append((request, timeout))
        return FakeResponse(body)

    return _urlopen


def download(body: bytes, calls=None, message_id="3EB0A1B2C3D4E5F6"):
    calls = [] if calls is None else calls
    with mock.patch("apps.whatsapp.client.urlopen", urlopen_returning(body, calls)):
        return EvolutionClient().download_audio(message_id, max_bytes=LIMIT)


def test_valid_media_is_decoded_and_request_carries_only_the_id():
    calls = []
    media = download(media_response(), calls)
    assert media.data == AUDIO
    assert media.declared_mimetype == "audio/ogg; codecs=opus"
    request, timeout = calls[0]
    assert request.full_url == "http://evolution.test/chat/getBase64FromMediaMessage/test-instance"
    assert request.get_method() == "POST"
    assert request.get_header("Apikey") == "test-evolution-api-key"
    assert json.loads(request.data) == {
        "message": {"key": {"id": "3EB0A1B2C3D4E5F6"}},
        "convertToMp4": False,
    }
    assert timeout == 5.0
    assert "passwd" not in repr(media)


@pytest.mark.parametrize("message_id", ["", "../x", "a b", "x" * 200, "http://evil/a"])
def test_invalid_message_id_never_calls_evolution(message_id):
    with mock.patch("apps.whatsapp.client.urlopen") as urlopen, pytest.raises(ValueError):
        EvolutionClient().download_audio(message_id, max_bytes=LIMIT)
    urlopen.assert_not_called()


def test_payload_urls_are_never_fetched():
    """Only the Evolution base URL is ever contacted, whatever the webhook carried."""
    calls = []
    download(media_response(url="https://attacker.example/x.ogg"), calls)
    assert len(calls) == 1
    assert calls[0][0].full_url.startswith("http://evolution.test/")


@pytest.mark.parametrize(
    "overrides",
    [
        {"base64": ""},
        {"base64": None},
        {"base64": "not*valid*base64!!"},
        {"mediaType": "imageMessage"},
        {"mediaType": "documentMessage"},
    ],
)
def test_invalid_media_rejected(overrides):
    with pytest.raises(EvolutionResponseError):
        download(media_response(**overrides))


def test_empty_response_body_rejected():
    with pytest.raises(EvolutionResponseError):
        download(b"")


def test_media_above_limit_rejected_before_decoding():
    big = base64.b64encode(b"OggS" + b"\x00" * (LIMIT + 10)).decode()
    with (
        mock.patch("apps.whatsapp.client.base64.b64decode") as decode,
        pytest.raises(MediaTooLargeError),
    ):
        download(media_response(base64=big))
    decode.assert_not_called()


def test_response_read_is_bounded():
    huge = b'{"mediaType": "audioMessage", "base64": "' + b"A" * 500_000 + b'"}'
    reads = []

    class Tracking(FakeResponse):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    with (
        mock.patch("apps.whatsapp.client.urlopen", lambda request, timeout=None: Tracking(huge)),
        pytest.raises(MediaTooLargeError),
    ):
        EvolutionClient().download_audio("ABC", max_bytes=LIMIT)
    assert reads and all(size > 0 for size in reads)  # never an unbounded read()


def test_file_exactly_at_limit_is_accepted():
    exact = b"OggS" + b"\x00" * (LIMIT - 4)
    media = download(media_response(base64=base64.b64encode(exact).decode()))
    assert len(media.data) == LIMIT


def test_evolution_http_error_and_timeout():
    error = HTTPError("x", 400, "bad", {}, io.BytesIO(b'{"response":{"message":["not found"]}}'))
    with (
        mock.patch("apps.whatsapp.client.urlopen", side_effect=error),
        pytest.raises(EvolutionHTTPError) as exc,
    ):
        EvolutionClient().download_audio("ABC", max_bytes=LIMIT)
    assert exc.value.status_code == 400
    with (
        mock.patch("apps.whatsapp.client.urlopen", side_effect=TimeoutError("t")),
        pytest.raises(EvolutionTimeoutError),
    ):
        EvolutionClient().download_audio("ABC", max_bytes=LIMIT)
