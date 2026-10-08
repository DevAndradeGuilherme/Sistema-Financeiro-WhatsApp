"""Minimal Supabase Storage client (REST, stdlib only) for the PRIVATE receipts bucket.

Server-side only: authenticates with ``SUPABASE_SERVICE_ROLE_KEY`` (``apikey`` +
``Authorization: Bearer``), which bypasses RLS and must never leave the backend (not stored,
not logged, not sent to any client or to OpenAI). Only a few endpoints are used:

- ``GET  /storage/v1/bucket/{bucket}``             bucket info (existence, ``public`` flag)
- ``POST /storage/v1/object/{bucket}/{path}``      upload, ``x-upsert: false`` (never overwrite)
- ``GET  /storage/v1/object/info/{bucket}/{path}`` object metadata (exists?)
- ``DELETE /storage/v1/object/{bucket}/{path}``    delete (used by the smoke check only)

No public URL is ever produced. A small client avoids pulling the full Supabase SDK
(auth/realtime/postgrest) for four HTTP calls.
"""

import json
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9/_.-]{0,255}$")


class StorageError(Exception):
    """Storage failure; ``code`` is safe to store and log."""

    def __init__(self, code: str, status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(code if status is None else f"{code} (HTTP {status})")


@dataclass(frozen=True)
class StorageConfig:
    url: str
    key: str
    bucket: str
    timeout: float

    def __repr__(self) -> str:  # never expose the key
        return f"StorageConfig(url={self.url!r}, bucket={self.bucket!r})"

    @classmethod
    def from_settings(cls) -> "StorageConfig":
        def required(name: str) -> str:
            value = getattr(settings, name, None)
            if value is None or str(value).strip() == "":
                raise ImproperlyConfigured(f"{name} is required for receipt storage.")
            return str(value).strip()

        url = required("SUPABASE_URL").rstrip("/")
        if urlparse(url).scheme != "https":
            raise ImproperlyConfigured("SUPABASE_URL must use https.")
        try:
            timeout = float(required("SUPABASE_TIMEOUT_SECONDS"))
        except ValueError as exc:
            raise ImproperlyConfigured("SUPABASE_TIMEOUT_SECONDS must be a number.") from exc
        if timeout <= 0:
            raise ImproperlyConfigured("SUPABASE_TIMEOUT_SECONDS must be positive.")
        return cls(
            url=url,
            key=required("SUPABASE_SERVICE_ROLE_KEY"),
            bucket=required("SUPABASE_STORAGE_BUCKET"),
            timeout=timeout,
        )


class StorageClient:
    def __init__(self, config: StorageConfig | None = None):
        self.config = config or StorageConfig.from_settings()

    def bucket_info(self) -> dict[str, Any]:
        return self._json("GET", f"/storage/v1/bucket/{quote(self.config.bucket, safe='')}")

    def upload(self, path: str, data: bytes, content_type: str) -> None:
        """Create the object; fails (``CONFLICT``) instead of overwriting an existing path."""
        self._call(
            "POST",
            self._object_url("object", path),
            data=data,
            headers={"Content-Type": content_type, "x-upsert": "false"},
        )
        logger.info("storage.upload bucket=%s bytes=%d", self.config.bucket, len(data))

    def object_info(self, path: str) -> dict[str, Any]:
        return self._json("GET", self._object_url("object/info", path))

    def delete(self, path: str) -> None:
        self._call("DELETE", self._object_url("object", path))

    # --- HTTP ----------------------------------------------------------------

    def _object_url(self, kind: str, path: str) -> str:
        if not _PATH_RE.match(path) or ".." in path:
            raise ValueError("Invalid storage path.")
        return f"/storage/v1/{kind}/{quote(self.config.bucket, safe='')}/{quote(path)}"

    def _json(self, method: str, path: str) -> dict[str, Any]:
        raw = self._call(method, path)
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            raise StorageError("INVALID_RESPONSE") from None
        if not isinstance(parsed, dict):
            raise StorageError("INVALID_RESPONSE")
        return parsed

    def _call(
        self, method: str, path: str, *, data: bytes | None = None, headers: dict | None = None
    ) -> bytes:
        all_headers = {
            "apikey": self.config.key,
            "Authorization": f"Bearer {self.config.key}",
            **(headers or {}),
        }
        # url scheme validated in StorageConfig; path built from validated parts only.
        request = Request(  # noqa: S310
            self.config.url + path, data=data, headers=all_headers, method=method
        )
        try:
            with urlopen(request, timeout=self.config.timeout) as response:  # noqa: S310
                return response.read(1024 * 1024)  # metadata responses only; bounded
        except HTTPError as exc:
            status = exc.code
            body = _small_body(exc)
            exc.close()
            code = _code_for(status, body)
            logger.warning("storage.http_error method=%s status=%s code=%s", method, status, code)
            raise StorageError(code, status) from None
        except TimeoutError:
            logger.warning("storage.timeout method=%s", method)
            raise StorageError("TIMEOUT") from None
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                logger.warning("storage.timeout method=%s", method)
                raise StorageError("TIMEOUT") from None
            logger.warning("storage.connection_error method=%s", method)
            raise StorageError("CONNECTION") from None


def _small_body(exc: HTTPError) -> dict:
    try:
        parsed = json.loads(exc.read(4096) or b"{}")
    except (ValueError, OSError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _code_for(status: int, body: dict | None = None) -> str:
    body = body or {}
    # Supabase Storage answers 400 with {"statusCode": "409", "error": "Duplicate"} on conflict.
    if status == 409 or str(body.get("statusCode")) == "409" or body.get("error") == "Duplicate":
        return "CONFLICT"
    if status in (401, 403):
        return "AUTH"
    if status == 404:
        return "NOT_FOUND"
    if status == 409:
        return "CONFLICT"
    if status >= 500:
        return "PROVIDER_ERROR"
    return "REJECTED"
