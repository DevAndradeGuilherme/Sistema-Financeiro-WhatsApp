"""OpenAI settings, validated when an interpretation is requested."""

from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

REASONING_EFFORTS = {"none", "low", "medium", "high"}


def _required(name: str) -> str:
    value = getattr(settings, name, None)
    if value is None or str(value).strip() == "":
        raise ImproperlyConfigured(f"{name} is required for AI interpretation.")
    return str(value).strip()


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    model: str
    timeout: float
    reasoning_effort: str | None

    def __repr__(self) -> str:  # never expose the API key
        return f"OpenAIConfig(model={self.model!r}, timeout={self.timeout!r})"

    @classmethod
    def from_settings(cls) -> "OpenAIConfig":
        try:
            timeout = float(_required("OPENAI_TIMEOUT_SECONDS"))
        except ValueError as exc:
            raise ImproperlyConfigured("OPENAI_TIMEOUT_SECONDS must be a number.") from exc
        if timeout <= 0:
            raise ImproperlyConfigured("OPENAI_TIMEOUT_SECONDS must be positive.")
        effort = (getattr(settings, "OPENAI_REASONING_EFFORT", None) or "").strip() or None
        if effort is not None and effort not in REASONING_EFFORTS:
            raise ImproperlyConfigured(
                f"OPENAI_REASONING_EFFORT must be one of {sorted(REASONING_EFFORTS)} or empty."
            )
        return cls(
            api_key=_required("OPENAI_API_KEY"),
            model=_required("OPENAI_MODEL"),
            timeout=timeout,
            reasoning_effort=effort,
        )


@dataclass(frozen=True)
class TranscriptionConfig:
    api_key: str
    model: str
    timeout: float

    def __repr__(self) -> str:  # never expose the API key
        return f"TranscriptionConfig(model={self.model!r}, timeout={self.timeout!r})"

    @classmethod
    def from_settings(cls) -> "TranscriptionConfig":
        try:
            timeout = float(_required("OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS"))
        except ValueError as exc:
            raise ImproperlyConfigured(
                "OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS must be a number."
            ) from exc
        if timeout <= 0:
            raise ImproperlyConfigured("OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS must be positive.")
        return cls(
            api_key=_required("OPENAI_API_KEY"),
            model=_required("OPENAI_TRANSCRIPTION_MODEL"),
            timeout=timeout,
        )
