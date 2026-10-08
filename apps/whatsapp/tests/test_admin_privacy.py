from html.parser import HTMLParser

import pytest
from django.utils import timezone

from apps.whatsapp.models import ConversationContext, InboundMessage, MessageType, ReceiptAttachment

pytestmark = pytest.mark.django_db


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in {"a", "img", "iframe", "source"}:
            self.links.extend(value for key, value in attrs if key in {"href", "src"})


def assert_read_only(client, base, record, data):
    assert client.get(base).status_code == 200
    response = client.get(f"{base}{record.pk}/change/")
    assert response.status_code == 200
    assert b'name="_save"' not in response.content
    assert client.get(f"{base}add/").status_code == 403
    assert client.post(f"{base}add/", data).status_code == 403
    assert client.post(f"{base}{record.pk}/change/", data).status_code == 403
    assert client.post(f"{base}{record.pk}/delete/", {"post": "yes"}).status_code == 403
    assert b"delete_selected" not in client.get(base).content
    return response.content.decode()


def test_context_is_read_only_even_for_superuser(admin_client, alice):
    query = {"scope": "SELF", "target_member_name": None, "months": 6, "trend_metric": "EXPENSE"}
    context = ConversationContext.objects.create(
        user=alice, last_query=query, expires_at=timezone.now()
    )
    assert_read_only(
        admin_client,
        "/admin/whatsapp/conversationcontext/",
        context,
        {"last_query": '{"scope":"FAMILY","months":12}'},
    )
    context.refresh_from_db()
    assert context.last_query == query


@pytest.mark.parametrize("message_type", [MessageType.TEXT, MessageType.AUDIO])
def test_message_contents_never_exposed_in_admin(admin_client, alice, message_type):
    inbound = InboundMessage.objects.create(
        user=alice,
        instance="test-instance",
        provider_message_id="privacy-dev",
        message_type=message_type,
        text="conteudo-sensivel-sintetico-que-nao-deve-aparecer",
    )
    html = assert_read_only(
        admin_client,
        "/admin/whatsapp/inboundmessage/",
        inbound,
        {"status": "READY", "intent": "CREATE_TRANSACTION", "transaction": "invalid"},
    )
    assert inbound.text not in html
    inbound.refresh_from_db()
    assert inbound.status == InboundMessage.Status.RECEIVED
    assert inbound.transaction_id is None and inbound.intent == ""


@pytest.mark.parametrize("legacy", [False, True])
def test_receipt_metadata_read_only_without_media_links(admin_client, legacy):
    inbound = InboundMessage.objects.create(
        instance="test-instance", provider_message_id="receipt-dev", message_type=MessageType.IMAGE
    )
    record = ReceiptAttachment.objects.create(
        inbound_message=inbound,
        status="PROCESSED",
        document_type="RECEIPT",
        content_type="image/png",
        byte_size=100,
        sha256="a" * 64,
        width=10,
        height=10,
        storage_bucket="legacy-dev" if legacy else None,
        storage_path="receipts/2026/01/synthetic.png" if legacy else None,
    )
    html = assert_read_only(
        admin_client,
        "/admin/whatsapp/receiptattachment/",
        record,
        {"storage_path": "receipts/changed.png", "status": "FAILED"},
    )
    assert "LEGACY" in html and record.sha256 in html
    parser = LinkParser()
    parser.feed(html)
    assert not any(
        "receipts/" in url or "storage" in url or "data:image" in url for url in parser.links
    )
    record.refresh_from_db()
    assert record.status == "PROCESSED"
    assert record.storage_path == ("receipts/2026/01/synthetic.png" if legacy else None)
