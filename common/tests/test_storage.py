import io
import json
from unittest import mock
from urllib.error import HTTPError, URLError

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from common.storage import StorageClient, StorageConfig, StorageError

KEY = "test-supabase-secret"


class FakeResponse:
    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def read(self, size=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture(body=b"{}"):
    calls = []

    def _urlopen(request, timeout=None):
        calls.append((request, timeout))
        return FakeResponse(body)

    return calls, _urlopen


def test_upload_is_private_server_side_and_never_overwrites():
    calls, fake = capture()
    with mock.patch("common.storage.urlopen", fake):
        StorageClient().upload("receipts/2026/10/abc123.jpg", b"\xff\xd8data", "image/jpeg")
    request, timeout = calls[0]
    assert request.get_method() == "POST"
    assert request.full_url == (
        "https://storage.test/storage/v1/object/test-receipts/receipts/2026/10/abc123.jpg"
    )
    assert request.get_header("Apikey") == KEY
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert request.get_header("X-upsert") == "false"
    assert request.get_header("Content-type") == "image/jpeg"
    assert request.data == b"\xff\xd8data"
    assert timeout == 5.0


def test_bucket_info_reports_privacy():
    calls, fake = capture(json.dumps({"id": "test-receipts", "public": False}).encode())
    with mock.patch("common.storage.urlopen", fake):
        assert StorageClient().bucket_info()["public"] is False
    assert calls[0][0].full_url == "https://storage.test/storage/v1/bucket/test-receipts"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../etc/passwd",
        "receipts/../../x",
        "/abs/path.jpg",
        "Receipts/A.JPG",
        "a b.jpg",
        "receipts/" + "x" * 300,
    ],
)
def test_unsafe_paths_rejected_without_request(path):
    with mock.patch("common.storage.urlopen") as urlopen, pytest.raises(ValueError):
        StorageClient().upload(path, b"x", "image/png")
    urlopen.assert_not_called()


def _http_error(status):
    return HTTPError("https://storage.test/x", status, "err", {}, io.BytesIO(b'{"error":"x"}'))


@pytest.mark.parametrize(
    "status, code",
    [
        (401, "AUTH"),
        (403, "AUTH"),
        (404, "NOT_FOUND"),
        (409, "CONFLICT"),
        (400, "REJECTED"),
        (413, "REJECTED"),
        (500, "PROVIDER_ERROR"),
        (503, "PROVIDER_ERROR"),
    ],
)
def test_http_errors_are_mapped(status, code):
    with (
        mock.patch("common.storage.urlopen", side_effect=_http_error(status)),
        pytest.raises(StorageError) as exc,
    ):
        StorageClient().upload("receipts/a.jpg", b"x", "image/jpeg")
    assert (exc.value.code, exc.value.status) == (code, status)
    assert KEY not in str(exc.value)


@pytest.mark.parametrize(
    "error", [TimeoutError("t"), TimeoutError("t"), URLError(TimeoutError("t"))]
)
def test_timeouts(error):
    with (
        mock.patch("common.storage.urlopen", side_effect=error),
        pytest.raises(StorageError) as exc,
    ):
        StorageClient().bucket_info()
    assert exc.value.code == "TIMEOUT"


def test_connection_error():
    with (
        mock.patch("common.storage.urlopen", side_effect=URLError("refused")),
        pytest.raises(StorageError) as exc,
    ):
        StorageClient().bucket_info()
    assert exc.value.code == "CONNECTION"


@pytest.mark.parametrize(
    "setting", ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_STORAGE_BUCKET"]
)
def test_missing_configuration_fails_clearly(setting):
    with override_settings(**{setting: None}), pytest.raises(ImproperlyConfigured, match=setting):
        StorageConfig.from_settings()


@override_settings(SUPABASE_URL="http://insecure.example")
def test_https_required():
    with pytest.raises(ImproperlyConfigured):
        StorageConfig.from_settings()


def test_key_never_in_repr():
    assert KEY not in repr(StorageConfig.from_settings())


def test_real_network_blocked_in_tests():
    from conftest import NetworkBlockedError

    with pytest.raises((NetworkBlockedError, StorageError)):
        StorageClient().bucket_info()


def test_supabase_duplicate_400_is_conflict():
    error = HTTPError(
        "https://storage.test/x",
        400,
        "Bad Request",
        {},
        io.BytesIO(b'{"statusCode":"409","error":"Duplicate","message":"exists"}'),
    )
    with (
        mock.patch("common.storage.urlopen", side_effect=error),
        pytest.raises(StorageError) as exc,
    ):
        StorageClient().upload("receipts/a.jpg", b"x", "image/jpeg")
    assert exc.value.code == "CONFLICT"
