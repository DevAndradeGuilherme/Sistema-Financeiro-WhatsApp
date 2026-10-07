"""Access to Evolution settings, failing clearly when a required value is missing."""

from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

PROVIDER = "EVOLUTION"
WEBHOOK_SECRET_HEADER = "X-Webhook-Secret"  # noqa: S105 - header name, not a secret
WEBHOOK_EVENTS = ("MESSAGES_UPSERT",)


def _required(name: str) -> str:
    value = getattr(settings, name, None)
    if value is None or str(value).strip() == "":
        raise ImproperlyConfigured(f"{name} is required for this WhatsApp/Evolution operation.")
    return str(value).strip()


def instance_name() -> str:
    return _required("EVOLUTION_INSTANCE_NAME")


def webhook_secret() -> str:
    return _required("EVOLUTION_WEBHOOK_SECRET")


@dataclass(frozen=True)
class EvolutionConfig:
    base_url: str
    api_key: str
    instance: str
    timeout: float

    def __repr__(self) -> str:  # never expose the API key
        return f"EvolutionConfig(base_url={self.base_url!r}, instance={self.instance!r})"

    @classmethod
    def from_settings(cls) -> "EvolutionConfig":
        base_url = _required("EVOLUTION_API_BASE_URL").rstrip("/")
        if urlparse(base_url).scheme not in {"http", "https"}:
            raise ImproperlyConfigured("EVOLUTION_API_BASE_URL must be an http(s) URL.")
        raw_timeout = _required("EVOLUTION_TIMEOUT_SECONDS")
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ImproperlyConfigured("EVOLUTION_TIMEOUT_SECONDS must be a number.") from exc
        if timeout <= 0:
            raise ImproperlyConfigured("EVOLUTION_TIMEOUT_SECONDS must be positive.")
        return cls(
            base_url=base_url,
            api_key=_required("EVOLUTION_API_KEY"),
            instance=instance_name(),
            timeout=timeout,
        )
