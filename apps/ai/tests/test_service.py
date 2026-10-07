import json
from datetime import date
from types import SimpleNamespace
from unittest import mock

import httpx
import openai
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.ai.contract import CatalogCategory, CatalogSubcategory, InterpretationContext
from apps.ai.prompts import build_instructions
from apps.ai.service import AIError, build_client, interpret_message

CONTEXT = InterpretationContext(
    reference_date=date(2026, 10, 6),
    timezone="America/Sao_Paulo",
    catalog=(
        CatalogCategory(
            id=2,
            name="Transporte",
            kind="EXPENSE",
            aliases=("carro",),
            subcategories=(CatalogSubcategory(id=1, name="Combustível", aliases=("gasolina",)),),
        ),
        CatalogCategory(id=12, name="Salário", kind="INCOME"),
    ),
)

OUTPUT = {
    "intent": "CREATE_TRANSACTION",
    "transaction_type": "EXPENSE",
    "amount": "10.00",
    "description": "gasolina",
    "category_id": 2,
    "subcategory_id": 1,
    "transaction_date": None,
    "query_kind": None,
    "start_date": None,
    "end_date": None,
}


def response(output=OUTPUT, status="completed", content_type="output_text", raw=None):
    text = raw if raw is not None else json.dumps(output)
    return SimpleNamespace(
        status=status,
        model="test-model-2026",
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type=content_type)])],
        output_text=text,
        usage=SimpleNamespace(
            input_tokens=420,
            output_tokens=60,
            total_tokens=480,
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
    )


def fake_client(result=None, error=None):
    client = mock.Mock()
    if error is not None:
        client.responses.create.side_effect = error
    else:
        client.responses.create.return_value = result or response()
    return client


def test_single_structured_call_with_minimal_data():
    client = fake_client()
    result = interpret_message("10 reais de gasolina", CONTEXT, client=client)

    client.responses.create.assert_called_once()
    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["model"] == "test-model"  # from settings, never hardcoded
    assert kwargs["store"] is False
    assert kwargs["input"] == "10 reais de gasolina"
    fmt = kwargs["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert "reasoning" not in kwargs  # only sent when configured
    assert "tools" not in kwargs
    assert "previous_response_id" not in kwargs
    assert "conversation" not in kwargs

    assert result.interpretation.amount == "10.00"
    assert result.model == "test-model-2026"
    assert result.usage == {
        "input_tokens": 420,
        "output_tokens": 60,
        "total_tokens": 480,
        "reasoning_tokens": 0,
    }


@override_settings(OPENAI_REASONING_EFFORT="low")
def test_reasoning_effort_sent_when_configured():
    client = fake_client()
    interpret_message("oi", CONTEXT, client=client)
    assert client.responses.create.call_args.kwargs["reasoning"] == {"effort": "low"}


@override_settings(OPENAI_REASONING_EFFORT="extreme")
def test_invalid_reasoning_effort_fails_clearly():
    with pytest.raises(ImproperlyConfigured):
        interpret_message("oi", CONTEXT, client=fake_client())


def test_instructions_contain_date_timezone_and_catalog_only():
    text = build_instructions(CONTEXT)
    assert "2026-10-06" in text
    assert "Tuesday" in text
    assert "America/Sao_Paulo" in text
    assert "category 2: Transporte [EXPENSE] (carro)" in text
    assert "subcategory 1: Combustível (gasolina)" in text
    assert len(text) < 3000  # keep the per-message prompt small


@pytest.mark.parametrize("setting", ["OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_TIMEOUT_SECONDS"])
def test_missing_configuration_fails_clearly(setting):
    with override_settings(**{setting: None}), pytest.raises(ImproperlyConfigured, match=setting):
        interpret_message("oi", CONTEXT, client=fake_client())


def test_sdk_client_never_retries():
    from apps.ai.conf import OpenAIConfig

    client = build_client(OpenAIConfig.from_settings())
    assert client.max_retries == 0
    assert client.timeout == 5.0


REQUEST = httpx.Request("POST", "https://api.openai.test/v1/responses")


def _status_error(cls, status):
    return cls("error", response=httpx.Response(status, request=REQUEST), body=None)


@pytest.mark.parametrize(
    "error, code",
    [
        (openai.APITimeoutError(request=REQUEST), "TIMEOUT"),
        (openai.APIConnectionError(request=REQUEST), "CONNECTION"),
        (_status_error(openai.AuthenticationError, 401), "AUTH"),
        (_status_error(openai.PermissionDeniedError, 403), "PERMISSION"),
        (_status_error(openai.RateLimitError, 429), "RATE_LIMIT"),
        (_status_error(openai.NotFoundError, 404), "MODEL_UNAVAILABLE"),
        (_status_error(openai.BadRequestError, 400), "BAD_REQUEST"),
        (_status_error(openai.InternalServerError, 500), "PROVIDER_ERROR"),
        (_status_error(openai.InternalServerError, 503), "PROVIDER_ERROR"),
    ],
)
def test_provider_errors_are_mapped(error, code):
    with pytest.raises(AIError) as exc:
        interpret_message("oi", CONTEXT, client=fake_client(error=error))
    assert exc.value.code == code


@pytest.mark.parametrize(
    "result, code",
    [
        (response(status="incomplete"), "INCOMPLETE"),
        (response(content_type="refusal"), "REFUSAL"),
        (response(raw=""), "EMPTY_OUTPUT"),
        (response(raw="not json"), "INVALID_OUTPUT"),
        (response(output={**OUTPUT, "target_user": "Maria"}), "INVALID_OUTPUT"),
        (response(output={**OUTPUT, "amount": 10.0}), "INVALID_OUTPUT"),
    ],
)
def test_bad_responses_are_rejected(result, code):
    with pytest.raises(AIError) as exc:
        interpret_message("oi", CONTEXT, client=fake_client(result))
    assert exc.value.code == code


@pytest.mark.parametrize("text", ["", "   ", "x" * 5000])
def test_invalid_input_never_calls_api(text):
    client = fake_client()
    with pytest.raises(AIError):
        interpret_message(text, CONTEXT, client=client)
    client.responses.create.assert_not_called()


def test_logs_do_not_contain_text_key_or_output(caplog):
    caplog.set_level("INFO")
    interpret_message("10 reais de gasolina segredo", CONTEXT, client=fake_client())
    logged = caplog.text
    assert "segredo" not in logged
    assert "test-openai-api-key" not in logged
    assert "10.00" not in logged
    assert "intent=CREATE_TRANSACTION" in logged
    assert "input_tokens=420" in logged
