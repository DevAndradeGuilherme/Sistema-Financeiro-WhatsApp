"""Speech-to-text for WhatsApp audio (V1.3). One transcription call per audio message.

Transcription and financial interpretation are separate steps: this module only turns audio
bytes into text in the original language (no translation, no cleanup by another model). The
text then goes through the existing textual pipeline (``apps.ai.service.interpret_message``).

- Model from ``OPENAI_TRANSCRIPTION_MODEL`` (never hardcoded, no fallback to another model).
- ``language="pt"`` (ISO-639-1) as a hint; no prompt, catalog or personal data is sent.
- Audio stays in memory; the SDK receives ``(filename, bytes, content_type)`` built by us.
- Logs carry only metrics (model, duration, bytes, usage) — never the transcript or audio.
"""

import logging
import time
from dataclasses import dataclass, field

import openai

from .conf import TranscriptionConfig
from .service import AIError, build_client, error_code

logger = logging.getLogger(__name__)

LANGUAGE = "pt"


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    model: str
    duration_ms: int
    usage: dict[str, float] = field(default_factory=dict)

    def __repr__(self) -> str:  # keep the transcript out of logs and tracebacks
        return f"TranscriptionResult(model={self.model!r}, chars={len(self.text)})"


def transcribe_audio(
    data: bytes,
    *,
    filename: str,
    content_type: str,
    client: openai.OpenAI | None = None,
    log_ref: str = "-",
) -> TranscriptionResult:
    """Transcribe ``data``. ``filename``/``content_type`` must be chosen by the caller from the
    detected format (never from external input). Raises ``AIError`` (sanitized code)."""
    if not data:
        raise AIError("EMPTY_AUDIO")
    config = TranscriptionConfig.from_settings()
    client = client or build_client(config)

    started = time.monotonic()
    try:
        response = client.audio.transcriptions.create(
            model=config.model,
            file=(filename, data, content_type),
            language=LANGUAGE,
            response_format="json",
        )
    except openai.OpenAIError as exc:
        code = error_code(exc)
        _log(log_ref, config.model, started, len(data), outcome=code)
        raise AIError(code) from None

    text = getattr(response, "text", None)
    if not isinstance(text, str):
        _log(log_ref, config.model, started, len(data), outcome="INVALID_OUTPUT")
        raise AIError("INVALID_OUTPUT")
    text = text.strip()
    if not text:
        _log(log_ref, config.model, started, len(data), outcome="EMPTY_TRANSCRIPT")
        raise AIError("EMPTY_TRANSCRIPT")

    usage = _usage(response)
    _log(log_ref, config.model, started, len(data), outcome="OK", usage=usage)
    return TranscriptionResult(
        text=text,
        model=config.model,
        duration_ms=int((time.monotonic() - started) * 1000),
        usage=usage,
    )


def _usage(response) -> dict[str, float]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    result: dict[str, float] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens", "seconds"):
        value = getattr(usage, name, None)
        if isinstance(value, int | float) and not isinstance(value, bool):
            result[name] = value
    details = getattr(usage, "input_token_details", None)
    audio_tokens = getattr(details, "audio_tokens", None)
    if isinstance(audio_tokens, int):
        result["audio_tokens"] = audio_tokens
    return result


def _log(ref: str, model: str, started: float, size: int, *, outcome: str, usage=None) -> None:
    usage = usage or {}
    logger.info(
        "ai.transcribe ref=%s model=%s outcome=%s duration_ms=%d bytes=%d input_tokens=%s "
        "output_tokens=%s audio_tokens=%s seconds=%s",
        ref,
        model,
        outcome,
        int((time.monotonic() - started) * 1000),
        size,
        usage.get("input_tokens", "-"),
        usage.get("output_tokens", "-"),
        usage.get("audio_tokens", "-"),
        usage.get("seconds", "-"),
    )
