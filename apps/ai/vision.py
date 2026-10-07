"""Receipt image → structured proposal (V1.4). ONE vision call per image.

- Responses API with ``input_image`` (data URL of the already-validated, metadata-free
  image held in memory) — no public URL, no Storage URL, no keys sent to OpenAI.
- ``detail="high"``: receipts are small text; ``high`` was sufficient in tests and cheaper
  than ``original``.
- Structured Outputs (strict JSON Schema), ``store=False``, ``max_retries=0``, no tools.
- No OCR step and no second model: the multimodal model reads the image directly.
- Logs carry only metrics, never the image, the extracted values or the description.
"""

import base64
import json
import logging
import time
from dataclasses import dataclass, field

import openai

from .conf import VisionConfig
from .contract import (
    RECEIPT_CONTRACT_VERSION,
    RECEIPT_SCHEMA,
    InterpretationContext,
    InvalidOutput,
    ReceiptExtraction,
    parse_receipt,
)
from .prompts import RECEIPT_PROMPT_VERSION, build_receipt_instructions
from .service import MAX_OUTPUT_TOKENS, AIError, build_client, error_code

logger = logging.getLogger(__name__)

DETAIL = "high"
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


@dataclass(frozen=True)
class VisionResult:
    extraction: ReceiptExtraction
    model: str
    duration_ms: int
    usage: dict[str, int] = field(default_factory=dict)


def extract_receipt(
    image: bytes,
    *,
    content_type: str,
    context: InterpretationContext,
    client: openai.OpenAI | None = None,
    log_ref: str = "-",
) -> VisionResult:
    if not image or content_type not in ALLOWED_CONTENT_TYPES:
        raise AIError("INVALID_INPUT")
    config = VisionConfig.from_settings()
    client = client or build_client(config)
    data_url = f"data:{content_type};base64,{base64.b64encode(image).decode('ascii')}"
    request = {
        "model": config.model,
        "instructions": build_receipt_instructions(context),
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_image", "image_url": data_url, "detail": DETAIL}],
            }
        ],
        "store": False,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "receipt",
                "schema": RECEIPT_SCHEMA,
                "strict": True,
            }
        },
    }
    if config.reasoning_effort:
        request["reasoning"] = {"effort": config.reasoning_effort}

    started = time.monotonic()
    try:
        response = client.responses.create(**request)
        extraction = _extract(response)
    except AIError as exc:
        _log(log_ref, config.model, started, len(image), outcome=exc.code)
        raise
    except openai.OpenAIError as exc:
        code = error_code(exc)
        _log(log_ref, config.model, started, len(image), outcome=code)
        raise AIError(code) from None
    finally:
        request.clear()  # drop every reference to the base64 copy of the image
        del data_url

    usage = _usage(response)
    _log(
        log_ref,
        config.model,
        started,
        len(image),
        outcome="OK",
        usage=usage,
        document_type=extraction.document_type,
    )
    return VisionResult(
        extraction=extraction,
        model=getattr(response, "model", None) or config.model,
        duration_ms=int((time.monotonic() - started) * 1000),
        usage=usage,
    )


def _extract(response) -> ReceiptExtraction:
    if getattr(response, "status", None) != "completed":
        raise AIError("INCOMPLETE")
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "refusal":
                raise AIError("REFUSAL")
    raw = getattr(response, "output_text", None)
    if not raw:
        raise AIError("EMPTY_OUTPUT")
    try:
        return parse_receipt(json.loads(raw))
    except (ValueError, InvalidOutput):
        raise AIError("INVALID_OUTPUT") from None


def _usage(response) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    result: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            result[name] = value
    return result


def _log(ref, model, started, size, *, outcome, usage=None, document_type="-") -> None:
    usage = usage or {}
    logger.info(
        "ai.vision ref=%s model=%s prompt=%s contract=%s detail=%s outcome=%s document_type=%s "
        "duration_ms=%d bytes=%d input_tokens=%s output_tokens=%s",
        ref,
        model,
        RECEIPT_PROMPT_VERSION,
        RECEIPT_CONTRACT_VERSION,
        DETAIL,
        outcome,
        document_type,
        int((time.monotonic() - started) * 1000),
        size,
        usage.get("input_tokens", "-"),
        usage.get("output_tokens", "-"),
    )
