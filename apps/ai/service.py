"""One OpenAI Responses API call per message → validated ``Interpretation``.

- Structured Outputs with a strict JSON Schema; no free-text parsing.
- ``store=False``; no conversations, tools or function calling.
- ``max_retries=0``: the SDK must not multiply calls; failures surface as ``AIError``.
- Never logs the API key, the prompt, the message text or the structured output.
"""

import json
import logging
import time
from dataclasses import dataclass, field

import openai

from .conf import OpenAIConfig
from .contract import (
    CONTRACT_VERSION,
    OUTPUT_SCHEMA,
    Interpretation,
    InterpretationContext,
    InvalidOutput,
    parse_output,
)
from .prompts import PROMPT_VERSION, build_instructions

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 4096
MAX_OUTPUT_TOKENS = 1024


class AIError(Exception):
    """Interpretation failed. ``code`` is safe to store and log."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class InterpretationResult:
    interpretation: Interpretation
    model: str
    duration_ms: int
    usage: dict[str, int] = field(default_factory=dict)


def build_client(config: OpenAIConfig) -> openai.OpenAI:
    return openai.OpenAI(api_key=config.api_key, timeout=config.timeout, max_retries=0)


def interpret_message(
    text: str,
    context: InterpretationContext,
    *,
    client: openai.OpenAI | None = None,
    log_ref: str = "-",
) -> InterpretationResult:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_CHARS:
        raise AIError("INVALID_INPUT")
    config = OpenAIConfig.from_settings()
    client = client or build_client(config)

    request = {
        "model": config.model,
        "instructions": build_instructions(context),
        "input": _input(text, context),
        "store": False,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "interpretation",
                "schema": OUTPUT_SCHEMA,
                "strict": True,
            }
        },
    }
    if config.reasoning_effort:
        request["reasoning"] = {"effort": config.reasoning_effort}

    started = time.monotonic()
    try:
        response = client.responses.create(**request)
        interpretation = _extract(response)
    except AIError as exc:
        _log(log_ref, config.model, started, outcome=exc.code)
        raise
    except openai.OpenAIError as exc:
        code = _error_code(exc)
        _log(log_ref, config.model, started, outcome=code)
        raise AIError(code) from None

    usage = _usage(response)
    result = InterpretationResult(
        interpretation=interpretation,
        model=getattr(response, "model", None) or config.model,
        duration_ms=_elapsed_ms(started),
        usage=usage,
    )
    _log(log_ref, config.model, started, outcome="OK", intent=interpretation.intent, usage=usage)
    return result


def _input(text: str, context: InterpretationContext):
    """Plain text, or [previous query (developer), message (user)] for follow-ups."""
    if not context.previous_query:
        return text
    previous = json.dumps(context.previous_query, separators=(",", ":"), ensure_ascii=False)
    return [
        {"role": "developer", "content": f"Previous query: {previous}"},
        {"role": "user", "content": text},
    ]


def _extract(response) -> Interpretation:
    status = getattr(response, "status", None)
    if status != "completed":
        raise AIError("INCOMPLETE")
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "refusal":
                raise AIError("REFUSAL")
    raw = getattr(response, "output_text", None)
    if not raw:
        raise AIError("EMPTY_OUTPUT")
    try:
        return parse_output(json.loads(raw))
    except (ValueError, InvalidOutput):
        raise AIError("INVALID_OUTPUT") from None


def _error_code(exc: Exception) -> str:
    # Order matters: APITimeoutError subclasses APIConnectionError.
    mapping = (
        (openai.APITimeoutError, "TIMEOUT"),
        (openai.APIConnectionError, "CONNECTION"),
        (openai.AuthenticationError, "AUTH"),
        (openai.PermissionDeniedError, "PERMISSION"),
        (openai.RateLimitError, "RATE_LIMIT"),
        (openai.NotFoundError, "MODEL_UNAVAILABLE"),
        (openai.BadRequestError, "BAD_REQUEST"),
        (openai.InternalServerError, "PROVIDER_ERROR"),
        (openai.APIStatusError, "PROVIDER_ERROR"),
    )
    for exc_type, code in mapping:
        if isinstance(exc, exc_type):
            return code
    return "UNEXPECTED"


def _usage(response) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    result = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            result[name] = value
    details = getattr(usage, "output_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None)
    if isinstance(reasoning, int):
        result["reasoning_tokens"] = reasoning
    return result


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _log(ref: str, model: str, started: float, *, outcome: str, intent: str = "-", usage=None):
    usage = usage or {}
    logger.info(
        "ai.interpret ref=%s model=%s prompt=%s contract=%s outcome=%s intent=%s "
        "duration_ms=%d input_tokens=%s output_tokens=%s",
        ref,
        model,
        PROMPT_VERSION,
        CONTRACT_VERSION,
        outcome,
        intent,
        _elapsed_ms(started),
        usage.get("input_tokens", "-"),
        usage.get("output_tokens", "-"),
    )
