import pytest
from django.db import connection
from django.test import override_settings


@pytest.mark.django_db
def test_row_level_security_enabled_on_all_tables():
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')
              AND NOT c.relrowsecurity
            """
        )
        assert cursor.fetchall() == []


def test_health_endpoint(client):
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_health_endpoint_rejects_post(client):
    assert client.post("/health/").status_code == 405
