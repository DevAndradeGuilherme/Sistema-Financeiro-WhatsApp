"""Liveness (/health/) and readiness (/ready/) endpoints."""

import logging
from unittest import mock

import pytest
from django.db import DatabaseError, OperationalError, connection
from django.db.utils import InterfaceError
from django.test.utils import CaptureQueriesContext

SENSITIVE_ERROR = (
    'connection to server at "db.secret-host.internal" (10.0.0.7), port 6543 failed: '
    'password authentication failed for user "app_user" password=hunter2'
)


def _broken_cursor(exc):
    return mock.patch.object(connection, "cursor", side_effect=exc)


# --- liveness ------------------------------------------------------------------------


def test_liveness_survives_database_outage(client):
    # Not marked django_db: pytest-django also fails the test on any database access.
    with _broken_cursor(OperationalError(SENSITIVE_ERROR)):
        response = client.get("/health/")
    assert response.status_code == 200
    assert response.content == b'{"status": "ok"}'


# --- readiness -----------------------------------------------------------------------


@pytest.mark.django_db
def test_readiness_with_database(client):
    response = client.get("/ready/")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.django_db
def test_readiness_is_a_short_read_only_query(client):
    with CaptureQueriesContext(connection) as queries:
        assert client.get("/ready/").status_code == 200
    sql = [q["sql"].upper() for q in queries.captured_queries]
    assert any("STATEMENT_TIMEOUT" in s for s in sql)
    assert all(s.lstrip().startswith(("SELECT", "SAVEPOINT", "RELEASE")) for s in sql), sql


@pytest.mark.django_db
@pytest.mark.parametrize(
    "exc", [OperationalError(SENSITIVE_ERROR), InterfaceError(SENSITIVE_ERROR), DatabaseError()]
)
def test_readiness_without_database(client, caplog, exc):
    with caplog.at_level(logging.WARNING), _broken_cursor(exc):
        response = client.get("/ready/")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    for leaked in ("secret-host", "10.0.0.7", "6543", "app_user", "hunter2", "password"):
        assert leaked not in response.content.decode()
        assert leaked not in caplog.text
    assert "readiness.failed" in caplog.text


@pytest.mark.django_db
def test_readiness_statement_timeout_is_not_ready(client):
    """A probe slower than the readiness timeout is cancelled by PostgreSQL → 503."""
    with (
        mock.patch("common.views.READINESS_STATEMENT_TIMEOUT_MS", 50),
        mock.patch("common.views.READINESS_QUERY", "SELECT pg_sleep(1)"),
    ):
        response = client.get("/ready/")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert connection.needs_rollback is False
    assert client.get("/ready/").status_code == 200


@pytest.mark.django_db
def test_readiness_never_calls_external_providers(client):
    with (
        mock.patch("apps.whatsapp.client.urlopen") as evolution,
        mock.patch("openai.OpenAI") as openai,
        mock.patch("httpx.Client.send") as httpx_send,
    ):
        assert client.get("/ready/").status_code == 200
    evolution.assert_not_called()
    openai.assert_not_called()
    httpx_send.assert_not_called()


def test_readiness_rejects_post(client):
    assert client.post("/ready/").status_code == 405
