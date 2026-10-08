import io
import json
from unittest import mock
from urllib.error import HTTPError, URLError

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.whatsapp.client import (
    EvolutionClient,
    EvolutionConnectionError,
    EvolutionHTTPError,
    EvolutionResponseError,
    EvolutionTimeoutError,
)
from apps.whatsapp.conf import EvolutionConfig
from conftest import NetworkBlockedError

API_KEY = "test-evolution-api-key"


class FakeResponse:
    def __init__(self, body: bytes, status: int = 201):
        self._body = body
        self.status = status

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(body: dict | list | bytes, calls: list):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    def _urlopen(request, timeout=None):
        calls.append((request, timeout))
        return FakeResponse(raw)

    return _urlopen


SEND_RESPONSE = {
    "key": {"remoteJid": "5531999990001@s.whatsapp.net", "fromMe": True, "id": "3EB0SENT"},
    "message": {"conversation": "oi"},
    "messageTimestamp": 1791331200,
    "status": "PENDING",
}


def test_send_text_posts_expected_payload_and_auth():
    calls = []
    with mock.patch("apps.whatsapp.client.urlopen", fake_urlopen(SEND_RESPONSE, calls)):
        sent = EvolutionClient().send_text("(31) 99999-0001", "Olá")

    assert sent.provider_message_id == "3EB0SENT"
    assert sent.status == "PENDING"
    request, timeout = calls[0]
    assert request.get_method() == "POST"
    assert request.full_url == "http://evolution.test/message/sendText/test-instance"
    assert request.get_header("Apikey") == API_KEY
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"number": "5531999990001", "text": "Olá"}
    assert timeout == 5.0


@pytest.mark.parametrize("phone", ["", "123", "abc"])
def test_send_text_rejects_invalid_phone_without_calling_api(phone):
    with mock.patch("apps.whatsapp.client.urlopen") as urlopen, pytest.raises(ValueError):
        EvolutionClient().send_text(phone, "Olá")
    urlopen.assert_not_called()


@pytest.mark.parametrize("text", ["", "   ", "x" * 4097])
def test_send_text_rejects_invalid_text(text):
    with mock.patch("apps.whatsapp.client.urlopen") as urlopen, pytest.raises(ValueError):
        EvolutionClient().send_text("+5531999990001", text)
    urlopen.assert_not_called()


@pytest.mark.parametrize("error", [TimeoutError("timed out"), TimeoutError("timed out")])
def test_timeout_is_translated(error):
    with (
        mock.patch("apps.whatsapp.client.urlopen", side_effect=error),
        pytest.raises(EvolutionTimeoutError),
    ):
        EvolutionClient().send_text("+5531999990001", "Olá")


def test_timeout_wrapped_in_urlerror_is_translated():
    with (
        mock.patch("apps.whatsapp.client.urlopen", side_effect=URLError(TimeoutError())),
        pytest.raises(EvolutionTimeoutError),
    ):
        EvolutionClient().get_server_info()


def test_connection_error_is_translated():
    with (
        mock.patch("apps.whatsapp.client.urlopen", side_effect=URLError("refused")),
        pytest.raises(EvolutionConnectionError),
    ):
        EvolutionClient().get_server_info()


def _http_error(status: int, body: dict):
    return HTTPError(
        "http://evolution.test/x", status, "error", {}, io.BytesIO(json.dumps(body).encode())
    )


@pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
def test_http_errors_are_translated_with_status(status):
    body = {"status": status, "error": "Bad", "response": {"message": ["instance not found"]}}
    with (
        mock.patch("apps.whatsapp.client.urlopen", side_effect=_http_error(status, body)),
        pytest.raises(EvolutionHTTPError) as exc,
    ):
        EvolutionClient().send_text("+5531999990001", "Olá")
    assert exc.value.status_code == status
    assert "instance not found" in str(exc.value)
    assert API_KEY not in str(exc.value)


def test_non_json_response_raises():
    with (
        mock.patch("apps.whatsapp.client.urlopen", fake_urlopen(b"<html>", [])),
        pytest.raises(EvolutionResponseError),
    ):
        EvolutionClient().get_server_info()


def test_success_response_read_is_bounded():
    huge = b"{" + b"x" * (2 * 1024 * 1024)
    reads = []

    class Tracking(FakeResponse):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    with (
        mock.patch("apps.whatsapp.client.urlopen", return_value=Tracking(huge)),
        pytest.raises(EvolutionResponseError, match="size limit"),
    ):
        EvolutionClient().get_server_info()
    assert reads == [1024 * 1024 + 1]


def test_http_error_body_read_is_bounded():
    reads = []

    class Tracking(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    error = HTTPError(
        "http://evolution.test/x",
        500,
        "error",
        {},
        Tracking(b'{"error":"' + b"x" * 100_000 + b'"}'),
    )
    with (
        mock.patch("apps.whatsapp.client.urlopen", side_effect=error),
        pytest.raises(EvolutionHTTPError),
    ):
        EvolutionClient().get_server_info()
    assert reads == [4096]


def test_set_webhook_sends_only_messages_upsert_with_secret_header():
    calls = []
    with mock.patch("apps.whatsapp.client.urlopen", fake_urlopen({"enabled": True}, calls)):
        EvolutionClient().set_webhook(url="https://example.test/hook/", secret="s3cr3t")
    request, _ = calls[0]
    assert request.full_url == "http://evolution.test/webhook/set/test-instance"
    assert json.loads(request.data) == {
        "webhook": {
            "enabled": True,
            "url": "https://example.test/hook/",
            "headers": {"X-Webhook-Secret": "s3cr3t"},
            "byEvents": False,
            "base64": False,
            "events": ["MESSAGES_UPSERT"],
        }
    }


def test_connection_state_is_parsed():
    body = {"instance": {"instanceName": "test-instance", "state": "open"}}
    with mock.patch("apps.whatsapp.client.urlopen", fake_urlopen(body, [])):
        assert EvolutionClient().get_connection_state() == "open"


def test_instance_name_is_url_encoded():
    calls = []
    with (
        override_settings(EVOLUTION_INSTANCE_NAME="inst/../x"),
        mock.patch("apps.whatsapp.client.urlopen", fake_urlopen({}, calls)),
    ):
        EvolutionClient().find_webhook()
    assert calls[0][0].full_url == "http://evolution.test/webhook/find/inst%2F..%2Fx"


@pytest.mark.parametrize(
    "setting",
    [
        "EVOLUTION_API_BASE_URL",
        "EVOLUTION_API_KEY",
        "EVOLUTION_INSTANCE_NAME",
        "EVOLUTION_TIMEOUT_SECONDS",
    ],
)
def test_missing_configuration_fails_clearly(setting):
    with override_settings(**{setting: None}), pytest.raises(ImproperlyConfigured, match=setting):
        EvolutionClient()


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_invalid_timeout_fails_clearly(value):
    with override_settings(EVOLUTION_TIMEOUT_SECONDS=value), pytest.raises(ImproperlyConfigured):
        EvolutionConfig.from_settings()


def test_config_repr_hides_api_key():
    assert API_KEY not in repr(EvolutionConfig.from_settings())


def test_real_network_is_blocked_in_tests():
    with pytest.raises((NetworkBlockedError, EvolutionConnectionError)):
        EvolutionClient().get_server_info()
