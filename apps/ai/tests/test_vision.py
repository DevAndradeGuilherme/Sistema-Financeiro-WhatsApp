import base64
import json
from datetime import date
from types import SimpleNamespace
from unittest import mock

import httpx
import openai
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.ai.contract import (
    RECEIPT_SCHEMA,
    CatalogCategory,
    CatalogSubcategory,
    InterpretationContext,
    InvalidOutput,
    parse_receipt,
)
from apps.ai.prompts import build_receipt_instructions
from apps.ai.service import AIError
from apps.ai.vision import extract_receipt

CONTEXT = InterpretationContext(
    reference_date=date(2026, 10, 7),
    timezone="America/Sao_Paulo",
    catalog=(
        CatalogCategory(
            id=2,
            name="Transporte",
            kind="EXPENSE",
            subcategories=(CatalogSubcategory(id=1, name="Combustível"),),
        ),
    ),
)
IMAGE = b"\xff\xd8\xff" + b"\x00" * 100
RECEIPT = {
    "is_financial_document": True,
    "document_type": "RECEIPT",
    "transaction_type": "EXPENSE",
    "amount": "10.00",
    "currency": "BRL",
    "transaction_date": "2026-10-07",
    "description": "posto de combustível",
    "category_id": 2,
    "subcategory_id": 1,
}
REQUEST = httpx.Request("POST", "https://api.openai.test/v1/responses")


def response(output=RECEIPT, status="completed", content_type="output_text", raw=None):
    return SimpleNamespace(
        status=status,
        model="test-vision-2026",
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type=content_type)])],
        output_text=raw if raw is not None else json.dumps(output),
        usage=SimpleNamespace(input_tokens=1800, output_tokens=90, total_tokens=1890),
    )


def fake_client(result=None, error=None):
    client = mock.Mock()
    if error is not None:
        client.responses.create.side_effect = error
    else:
        client.responses.create.return_value = result or response()
    return client


def call(client, image=IMAGE, content_type="image/jpeg"):
    return extract_receipt(image, content_type=content_type, context=CONTEXT, client=client)


def test_single_call_with_inline_image_and_strict_schema():
    client = fake_client()
    result = call(client)
    client.responses.create.assert_called_once()
    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["model"] == "test-vision"  # OPENAI_VISION_MODEL, never hardcoded
    assert kwargs["store"] is False
    assert "tools" not in kwargs
    fmt = kwargs["text"]["format"]
    assert (fmt["type"], fmt["strict"], fmt["schema"]) == ("json_schema", True, RECEIPT_SCHEMA)
    part = kwargs["input"][0]["content"][0]
    assert part["type"] == "input_image"
    assert part["detail"] == "high"
    assert part["image_url"] == "data:image/jpeg;base64," + base64.b64encode(IMAGE).decode()
    assert "supabase" not in json.dumps(kwargs["input"]).lower()  # no Storage URL / key
    assert result.extraction.amount == "10.00"
    assert result.usage == {"input_tokens": 1800, "output_tokens": 90, "total_tokens": 1890}


def test_instructions_cover_receipt_semantics():
    text = build_receipt_instructions(CONTEXT)
    for rule in (
        "FINAL TOTAL",
        "subtotal",
        "troco",
        "CPF",
        "CNPJ",
        "Pix key",
        "Pix enviado",
        "Pix recebido",
        "UNKNOWN",
        "Never guess",
        "category 2: Transporte",
    ):
        assert rule in text
    assert len(text) < 4000


@pytest.mark.parametrize("content_type", ["image/gif", "application/pdf", "text/plain", ""])
def test_unsupported_input_never_calls_api(content_type):
    client = fake_client()
    with pytest.raises(AIError):
        call(client, content_type=content_type)
    client.responses.create.assert_not_called()


def _status(cls, status):
    return cls("error", response=httpx.Response(status, request=REQUEST), body=None)


@pytest.mark.parametrize(
    "error, code",
    [
        (openai.APITimeoutError(request=REQUEST), "TIMEOUT"),
        (_status(openai.AuthenticationError, 401), "AUTH"),
        (_status(openai.RateLimitError, 429), "RATE_LIMIT"),
        (_status(openai.BadRequestError, 400), "BAD_REQUEST"),
        (_status(openai.InternalServerError, 500), "PROVIDER_ERROR"),
        (_status(openai.NotFoundError, 404), "MODEL_UNAVAILABLE"),
    ],
)
def test_provider_errors(error, code):
    with pytest.raises(AIError) as exc:
        call(fake_client(error=error))
    assert exc.value.code == code


@pytest.mark.parametrize(
    "result, code",
    [
        (response(status="incomplete"), "INCOMPLETE"),
        (response(content_type="refusal"), "REFUSAL"),
        (response(raw="not json"), "INVALID_OUTPUT"),
        (response(output={**RECEIPT, "amount": 10.0}), "INVALID_OUTPUT"),
        (response(output={**RECEIPT, "pix_key": "x"}), "INVALID_OUTPUT"),
        (response(output={**RECEIPT, "currency": "USD"}), "INVALID_OUTPUT"),
    ],
)
def test_bad_outputs(result, code):
    with pytest.raises(AIError) as exc:
        call(fake_client(result))
    assert exc.value.code == code


def test_schema_is_strict_and_has_no_personal_fields():
    assert RECEIPT_SCHEMA["additionalProperties"] is False
    assert set(RECEIPT_SCHEMA["required"]) == set(RECEIPT_SCHEMA["properties"])
    forbidden = {"cpf", "cnpj", "pix_key", "payer", "payee", "merchant_or_counterparty", "name"}
    assert not forbidden & set(RECEIPT_SCHEMA["properties"])
    assert RECEIPT_SCHEMA["properties"]["amount"]["type"] == ["string", "null"]


def test_parse_receipt_rejects_missing_fields():
    data = dict(RECEIPT)
    del data["currency"]
    with pytest.raises(InvalidOutput):
        parse_receipt(data)


@pytest.mark.parametrize("setting", ["OPENAI_VISION_MODEL", "OPENAI_VISION_TIMEOUT_SECONDS"])
def test_missing_configuration_fails_clearly(setting):
    with override_settings(**{setting: None}), pytest.raises(ImproperlyConfigured, match=setting):
        call(fake_client())


def test_logs_have_metrics_only(caplog):
    caplog.set_level("INFO")
    call(fake_client(response(output={**RECEIPT, "description": "farmacia segredo"})))
    assert "segredo" not in caplog.text and "10.00" not in caplog.text
    assert base64.b64encode(IMAGE).decode()[:20] not in caplog.text
    assert "ai.vision" in caplog.text and "document_type=RECEIPT" in caplog.text
    assert "detail=high" in caplog.text
