"""HTTP client for Evolution API v2 (contract in docs/WHATSAPP.md).

Uses only the standard library (urllib). Every call has a timeout. The API key is sent in the
``apikey`` header and never appears in exceptions, reprs or logs.
"""

import json
import logging
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from common.phone import InvalidPhoneNumber, normalize_phone

from .conf import WEBHOOK_EVENTS, WEBHOOK_SECRET_HEADER, EvolutionConfig

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 4096
_MAX_ERROR_DETAIL = 200


class EvolutionError(Exception):
    """Base error for Evolution API calls."""


class EvolutionTimeoutError(EvolutionError):
    pass


class EvolutionConnectionError(EvolutionError):
    pass


class EvolutionHTTPError(EvolutionError):
    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Evolution API returned HTTP {status_code}: {detail}".rstrip(": "))


class EvolutionResponseError(EvolutionError):
    """Successful status but unexpected body."""


@dataclass(frozen=True)
class SentMessage:
    provider_message_id: str | None
    status: str | None


class EvolutionClient:
    def __init__(self, config: EvolutionConfig | None = None):
        self.config = config or EvolutionConfig.from_settings()

    # --- Public operations ---------------------------------------------------

    def get_server_info(self) -> dict[str, Any]:
        """GET / — version and liveness; also validates connectivity."""
        return self._request("GET", "/")

    def get_connection_state(self) -> str | None:
        data = self._request("GET", f"/instance/connectionState/{self._instance()}")
        instance = data.get("instance") if isinstance(data.get("instance"), dict) else {}
        return instance.get("state")

    def send_text(self, phone: str, text: str) -> SentMessage:
        """Send a text message to a phone number (normalized through common.phone)."""
        try:
            canonical = normalize_phone(phone)
        except InvalidPhoneNumber as exc:
            raise ValueError("Invalid destination phone number.") from exc
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text must not be empty.")
        if len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"Text exceeds {MAX_TEXT_LENGTH} characters.")

        data = self._request(
            "POST",
            f"/message/sendText/{self._instance()}",
            {"number": canonical.lstrip("+"), "text": text},
        )
        key = data.get("key") if isinstance(data.get("key"), dict) else {}
        sent = SentMessage(provider_message_id=key.get("id"), status=data.get("status"))
        logger.info(
            "evolution.send_text instance=%s provider_message_id=%s status=%s",
            self.config.instance,
            sent.provider_message_id,
            sent.status,
        )
        return sent

    def find_webhook(self) -> dict[str, Any]:
        """Current webhook config. Contains the secret header: never print it as is."""
        return self._request("GET", f"/webhook/find/{self._instance()}")

    def set_webhook(self, *, url: str, secret: str) -> dict[str, Any]:
        """Idempotent upsert of the instance webhook: only MESSAGES_UPSERT, secret header."""
        body = {
            "webhook": {
                "enabled": True,
                "url": url,
                "headers": {WEBHOOK_SECRET_HEADER: secret},
                "byEvents": False,
                "base64": False,
                "events": list(WEBHOOK_EVENTS),
            }
        }
        return self._request("POST", f"/webhook/set/{self._instance()}", body)

    # --- HTTP ----------------------------------------------------------------

    def _instance(self) -> str:
        return quote(self.config.instance, safe="")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        headers = {"apikey": self.config.api_key, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        # base_url scheme is restricted to http(s) in EvolutionConfig.from_settings.
        url = self.config.base_url + path
        request = Request(url, data=data, headers=headers, method=method)  # noqa: S310

        try:
            with urlopen(request, timeout=self.config.timeout) as response:  # noqa: S310
                raw = response.read()
        except HTTPError as exc:
            detail = _error_detail(exc)
            exc.close()
            logger.warning("evolution.http_error method=%s status=%s", method, exc.code)
            raise EvolutionHTTPError(exc.code, detail) from None
        except TimeoutError as exc:
            logger.warning("evolution.timeout method=%s", method)
            raise EvolutionTimeoutError("Evolution API request timed out.") from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                logger.warning("evolution.timeout method=%s", method)
                raise EvolutionTimeoutError("Evolution API request timed out.") from None
            logger.warning("evolution.connection_error method=%s", method)
            raise EvolutionConnectionError("Could not connect to Evolution API.") from None

        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise EvolutionResponseError("Evolution API returned a non-JSON body.") from exc
        if isinstance(parsed, list):
            return {"items": parsed}
        if not isinstance(parsed, dict):
            raise EvolutionResponseError("Evolution API returned an unexpected body.")
        return parsed


def _error_detail(exc: HTTPError) -> str:
    """Short message from an Evolution error body ({"response": {"message": [...]}})."""
    try:
        body = json.loads(exc.read() or b"{}")
    except (ValueError, OSError):
        return ""
    if not isinstance(body, dict):
        return ""
    response = body.get("response")
    message = response.get("message") if isinstance(response, dict) else body.get("message")
    if isinstance(message, list):
        message = "; ".join(str(m) for m in message)
    return str(message or body.get("error") or "")[:_MAX_ERROR_DETAIL]
