from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.whatsapp.client import EvolutionConnectionError, SentMessage
from apps.whatsapp.models import InboundMessage, MessageType

pytestmark = pytest.mark.django_db

SECRET = "test-webhook-secret"
API_KEY = "test-evolution-api-key"


def test_admin_is_read_only(admin_client):
    record = InboundMessage.objects.create(
        instance="test-instance", provider_message_id="ABC", message_type=MessageType.TEXT
    )
    assert admin_client.get("/admin/whatsapp/inboundmessage/").status_code == 200
    assert (
        admin_client.get(f"/admin/whatsapp/inboundmessage/{record.pk}/change/").status_code == 200
    )
    assert admin_client.get("/admin/whatsapp/inboundmessage/add/").status_code == 403
    response = admin_client.post(
        f"/admin/whatsapp/inboundmessage/{record.pk}/change/", {"status": "PROCESSED"}
    )
    assert response.status_code == 403
    delete_url = f"/admin/whatsapp/inboundmessage/{record.pk}/delete/"
    assert admin_client.post(delete_url, {"post": "yes"}).status_code == 403
    record.refresh_from_db()
    assert record.status == InboundMessage.Status.RECEIVED


def _run(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out)
    return out.getvalue()


def test_evolution_check_reports_without_secrets():
    with mock.patch("apps.whatsapp.management.commands.evolution_check.EvolutionClient") as cls:
        client = cls.return_value
        client.config.instance = "test-instance"
        client.get_server_info.return_value = {"version": "2.3.7"}
        client.get_connection_state.return_value = "open"
        client.find_webhook.return_value = {
            "enabled": True,
            "url": "https://example.test/api/v1/webhooks/evolution/",
            "events": ["MESSAGES_UPSERT"],
            "headers": {"X-Webhook-Secret": SECRET},
        }
        output = _run("evolution_check")
    assert "2.3.7" in output
    assert "confere" in output
    assert SECRET not in output
    assert API_KEY not in output


def test_evolution_check_detects_wrong_secret():
    with mock.patch("apps.whatsapp.management.commands.evolution_check.EvolutionClient") as cls:
        client = cls.return_value
        client.config.instance = "test-instance"
        client.get_server_info.return_value = {"version": "2.3.7"}
        client.find_webhook.return_value = {"enabled": True, "headers": {"X-Webhook-Secret": "x"}}
        output = _run("evolution_check")
    assert "AUSENTE OU DIFERENTE" in output


def test_evolution_check_unreachable():
    with mock.patch("apps.whatsapp.management.commands.evolution_check.EvolutionClient") as cls:
        cls.return_value.get_server_info.side_effect = EvolutionConnectionError("down")
        with pytest.raises(CommandError):
            _run("evolution_check")


def test_set_webhook_command_uses_configured_secret():
    path = "apps.whatsapp.management.commands.evolution_set_webhook.EvolutionClient"
    with mock.patch(path) as cls:
        cls.return_value.find_webhook.return_value = {
            "url": "https://example.test/x/",
            "events": ["MESSAGES_UPSERT"],
            "enabled": True,
        }
        output = _run("evolution_set_webhook", "--url", "https://example.test/x/")
    cls.return_value.set_webhook.assert_called_once_with(
        url="https://example.test/x/", secret=SECRET
    )
    assert SECRET not in output


def test_set_webhook_command_rejects_invalid_url():
    with pytest.raises(CommandError):
        _run("evolution_set_webhook", "--url", "ftp://x")


def test_send_test_command():
    path = "apps.whatsapp.management.commands.evolution_send_test.EvolutionClient"
    with mock.patch(path) as cls:
        cls.return_value.send_text.return_value = SentMessage("ID1", "PENDING")
        output = _run("evolution_send_test", "--phone", "31999990001", "--text", "teste")
    cls.return_value.send_text.assert_called_once_with("31999990001", "teste")
    assert "ID1" in output
