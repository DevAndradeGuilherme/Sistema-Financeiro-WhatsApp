from types import SimpleNamespace
from unittest import mock

import httpx
import openai
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.ai.service import AIError
from apps.ai.transcription import transcribe_audio

AUDIO = b"OggS" + b"\x00" * 200
REQUEST = httpx.Request("POST", "https://api.openai.test/v1/audio/transcriptions")


def fake_client(text="gastei dez reais de gasolina", error=None, usage=None):
    client = mock.Mock()
    if error is not None:
        client.audio.transcriptions.create.side_effect = error
    else:
        client.audio.transcriptions.create.return_value = SimpleNamespace(
            text=text,
            usage=usage
            or SimpleNamespace(
                type="tokens",
                input_tokens=60,
                output_tokens=8,
                total_tokens=68,
                input_token_details=SimpleNamespace(audio_tokens=55, text_tokens=5),
            ),
        )
    return client


def call(client, data=AUDIO):
    return transcribe_audio(
        data, filename="audio.ogg", content_type="audio/ogg", client=client, log_ref="ref"
    )


def test_transcribes_with_configured_model_and_portuguese_hint():
    client = fake_client("  gastei dez reais de gasolina \n")
    result = call(client)
    assert result.text == "gastei dez reais de gasolina"
    assert result.model == "test-transcribe"
    assert result.usage == {
        "input_tokens": 60,
        "output_tokens": 8,
        "total_tokens": 68,
        "audio_tokens": 55,
    }
    kwargs = client.audio.transcriptions.create.call_args.kwargs
    assert kwargs["model"] == "test-transcribe"  # from settings, never hardcoded
    assert kwargs["language"] == "pt"
    assert kwargs["response_format"] == "json"
    assert kwargs["file"] == ("audio.ogg", AUDIO, "audio/ogg")
    assert "prompt" not in kwargs  # no catalog / personal data sent to the transcriber
    client.audio.transcriptions.create.assert_called_once()  # exactly one call


def test_duration_based_usage_is_reported():
    client = fake_client(usage=SimpleNamespace(type="duration", seconds=4))
    assert call(client).usage == {"seconds": 4}


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_transcript_rejected(text):
    with pytest.raises(AIError) as exc:
        call(fake_client(text))
    assert exc.value.code == "EMPTY_TRANSCRIPT"


def test_non_text_output_rejected():
    client = mock.Mock()
    client.audio.transcriptions.create.return_value = SimpleNamespace(text=None)
    with pytest.raises(AIError) as exc:
        call(client)
    assert exc.value.code == "INVALID_OUTPUT"


def test_zero_bytes_never_calls_api():
    client = fake_client()
    with pytest.raises(AIError) as exc:
        call(client, data=b"")
    assert exc.value.code == "EMPTY_AUDIO"
    client.audio.transcriptions.create.assert_not_called()


def _status(cls, status):
    return cls("error", response=httpx.Response(status, request=REQUEST), body=None)


@pytest.mark.parametrize(
    "error, code",
    [
        (openai.APITimeoutError(request=REQUEST), "TIMEOUT"),
        (openai.APIConnectionError(request=REQUEST), "CONNECTION"),
        (_status(openai.AuthenticationError, 401), "AUTH"),
        (_status(openai.PermissionDeniedError, 403), "PERMISSION"),
        (_status(openai.RateLimitError, 429), "RATE_LIMIT"),
        (_status(openai.BadRequestError, 400), "BAD_REQUEST"),  # invalid/unsupported file
        (_status(openai.NotFoundError, 404), "MODEL_UNAVAILABLE"),
        (_status(openai.InternalServerError, 500), "PROVIDER_ERROR"),
        (_status(openai.InternalServerError, 503), "PROVIDER_ERROR"),
    ],
)
def test_provider_errors_are_mapped(error, code):
    with pytest.raises(AIError) as exc:
        call(fake_client(error=error))
    assert exc.value.code == code


@pytest.mark.parametrize(
    "setting",
    ["OPENAI_API_KEY", "OPENAI_TRANSCRIPTION_MODEL", "OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS"],
)
def test_missing_configuration_fails_clearly(setting):
    with override_settings(**{setting: None}), pytest.raises(ImproperlyConfigured, match=setting):
        call(fake_client())


def test_logs_never_contain_transcript_audio_or_key(caplog):
    caplog.set_level("INFO")
    call(fake_client("gastei dez reais de gasolina segredo"))
    assert "segredo" not in caplog.text
    assert "test-openai-api-key" not in caplog.text
    assert "OggS" not in caplog.text
    assert "ai.transcribe" in caplog.text and "bytes=204" in caplog.text


def test_result_repr_hides_transcript():
    result = call(fake_client("segredo financeiro"))
    assert "segredo" not in repr(result)
