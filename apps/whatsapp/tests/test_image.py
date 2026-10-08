"""V1.4: receipt image → download → normalize → private Storage → vision → domain.

Mocked boundaries: Evolution download (``image.EvolutionClient``), Storage
(``image.StorageClient``), vision (``image.extract_receipt``) and the reply
(``pipeline.EvolutionClient``). Image validation (Pillow), Django rules, the financial domain,
idempotency and the database are real.
"""

import io
import json
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.db import IntegrityError, connections
from django.utils import timezone
from PIL import Image

from apps.ai.contract import ReceiptExtraction
from apps.ai.service import AIError
from apps.ai.vision import VisionResult
from apps.transactions.models import Transaction, TransactionChange
from apps.whatsapp import messages
from apps.whatsapp.client import DownloadedMedia, EvolutionHTTPError, MediaTooLargeError
from apps.whatsapp.models import InboundMessage, ReceiptAttachment
from apps.whatsapp.parsers import parse_evolution_event
from apps.whatsapp.services import receive_message
from common.dates import today_local
from common.storage import StorageError

from . import payloads, replies
from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db

URL = "/api/v1/webhooks/evolution/"
SECRET = "test-webhook-secret"
Status = InboundMessage.Status
AStatus = ReceiptAttachment.Status


def jpeg_with_gps() -> bytes:
    exif = Image.Exif()
    exif[0x8825] = {1: "S", 2: (23.0, 33.0, 0.0)}
    exif[0x010F] = "PhoneCam"
    buffer = io.BytesIO()
    Image.new("RGB", (400, 600), (255, 255, 255)).save(buffer, "JPEG", exif=exif.tobytes())
    return buffer.getvalue()


JPEG = jpeg_with_gps()


def receipt(**overrides):
    base = {
        "is_financial_document": True,
        "document_type": "RECEIPT",
        "transaction_type": "EXPENSE",
        "amount": "47.90",
        "currency": "BRL",
        "transaction_date": today_local().isoformat(),
        "description": "mercado",
        "category_id": None,
        "subcategory_id": None,
    }
    base.update(overrides)
    return ReceiptExtraction(**base)


def image_payload(message_id="IMG1", file_length=None, **kwargs):
    image = {
        "mimetype": "image/jpeg",
        "caption": "",
        "url": "https://attacker.example/x.jpg",
        "fileName": "../../joao-cpf.jpg",
    }
    if file_length is not None:
        image["fileLength"] = file_length
    return payloads.upsert(
        message_id=message_id,
        message_type="imageMessage",
        message={"imageMessage": image},
        **kwargs,
    )


def post_image(client, **kwargs):
    response = client.post(
        URL,
        data=json.dumps(image_payload(**kwargs)),
        content_type="application/json",
        headers={"X-Webhook-Secret": SECRET},
    )
    assert response.status_code == 200
    return InboundMessage.objects.get(provider_message_id=kwargs.get("message_id", "IMG1"))


@pytest.fixture
def download():
    with mock.patch("apps.whatsapp.image.EvolutionClient") as cls:
        cls.return_value.download_image.return_value = DownloadedMedia(JPEG, "image/jpeg")
        yield cls.return_value.download_image


@pytest.fixture
def storage():
    with mock.patch("apps.whatsapp.image.StorageClient") as cls:
        cls.return_value.config.bucket = "test-receipts"
        yield cls.return_value


@pytest.fixture
def vision(catalog):
    with mock.patch("apps.whatsapp.image.extract_receipt") as fn:
        fn.extract = lambda **kw: setattr(
            fn,
            "return_value",
            VisionResult(extraction=receipt(**kw), model="test-vision", duration_ms=5),
        )
        fn.extract(category_id=catalog["alimentacao"].pk)
        yield fn


def reply(evolution):
    return evolution.call_args.args[1]


# --- Happy paths -------------------------------------------------------------------------------


def test_expense_receipt_creates_transaction_and_links_attachment(
    client, alice, catalog, evolution, download, storage, vision
):
    record = post_image(client)

    download.assert_called_once_with("IMG1", max_bytes=1024 * 1024)
    storage.upload.assert_called_once()
    path, data, content_type = storage.upload.call_args.args
    vision.assert_called_once()
    assert vision.call_args.args[0] == data  # same normalized bytes stored and analysed
    assert content_type == "image/jpeg"
    assert b"PhoneCam" not in data and b"GPS" not in data  # EXIF/GPS removed before Storage

    txn = Transaction.objects.get()
    assert (txn.user, txn.type, txn.amount, txn.category, txn.source) == (
        alice,
        "EXPENSE",
        Decimal("47.90"),
        catalog["alimentacao"],
        "IMAGE",
    )
    attachment = ReceiptAttachment.objects.get()
    assert (attachment.inbound_message, attachment.transaction, attachment.status) == (
        record,
        txn,
        AStatus.PROCESSED,
    )
    assert attachment.storage_path == path
    assert attachment.document_type == "RECEIPT"
    assert (attachment.width, attachment.height, attachment.byte_size) == (400, 600, len(data))
    record.refresh_from_db()
    assert (record.status, record.transaction, record.intent) == (
        Status.PROCESSED,
        txn,
        "CREATE_TRANSACTION",
    )
    text = reply(evolution)
    assert replies.card(text) == replies.expense("47,90", "Alimentação", txn.transaction_date)
    assert text.endswith(messages.RECEIPT_STORED_SUFFIX)


def test_storage_path_is_opaque(client, alice, evolution, download, storage, vision):
    post_image(client)
    path = storage.upload.call_args.args[0]
    import re

    assert re.fullmatch(r"receipts/\d{4}/\d{2}/[0-9a-f]{32}\.jpg", path)
    for private in ("alice", "5531999990001", "joao", "cpf", "mercado"):
        assert private not in path


def test_pix_received_creates_income(client, alice, catalog, evolution, download, storage, vision):
    vision.extract(
        document_type="PAYMENT_PROOF",
        transaction_type="INCOME",
        amount="150.00",
        description="pix recebido",
    )
    post_image(client)
    txn = Transaction.objects.get()
    assert (txn.type, txn.amount, txn.category) == (
        "INCOME",
        Decimal("150.00"),
        catalog["outras_receitas"],
    )  # fallback by flag
    fields = replies.card(reply(evolution))
    assert (fields["title"], fields["Valor"]) == ("✅ Receita registrada", "R$ 150,00")


def test_pix_paid_creates_expense(client, alice, catalog, evolution, download, storage, vision):
    vision.extract(document_type="PAYMENT_PROOF", transaction_type="EXPENSE", amount="80.00")
    post_image(client)
    assert Transaction.objects.get().type == "EXPENSE"


def test_missing_printed_date_uses_message_date(
    client, alice, evolution, download, storage, vision
):
    vision.extract(transaction_date=None)
    record = post_image(client)
    # The fixture's messageTimestamp is 2026-10-07T00:00Z = 2026-10-06 21:00 in São Paulo:
    # the business-timezone date of *receipt* is used (never "today" by accident).
    expected = timezone.localtime(record.provider_timestamp).date()
    assert expected.isoformat() == "2026-10-06"
    assert Transaction.objects.get().transaction_date == expected


def test_undo_by_text_soft_deletes_but_keeps_receipt(
    client, alice, catalog, ai, evolution, download, storage, vision
):
    post_image(client)
    ai.set(interp(intent="UNDO_TRANSACTION"))
    deliver(client, "desfazer", "T1")
    txn = Transaction.objects.get()
    assert txn.deleted_at is not None
    attachment = ReceiptAttachment.objects.get()
    assert attachment.transaction == txn  # evidence kept, still linked
    storage.delete.assert_not_called()


# --- No financial effect ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, code, message",
    [
        (
            {
                "is_financial_document": False,
                "document_type": "OTHER",
                "transaction_type": "UNKNOWN",
                "amount": None,
                "currency": "UNKNOWN",
            },
            "NOT_A_RECEIPT",
            messages.NOT_A_RECEIPT,
        ),
        (
            {"amount": None},
            "AMOUNT_UNREADABLE",
            messages.RECEIPT_AMOUNT_UNREADABLE,
        ),
        (
            {"currency": "OTHER"},
            "FOREIGN_CURRENCY",
            messages.RECEIPT_FOREIGN_CURRENCY,
        ),
        ({"currency": "UNKNOWN"}, "CURRENCY_UNCLEAR", None),
        (
            {"document_type": "PAYMENT_PROOF", "transaction_type": "UNKNOWN"},
            "DIRECTION_UNCLEAR",
            messages.RECEIPT_DIRECTION_UNCLEAR,
        ),
        ({"amount": "abc"}, "AMOUNT_UNREADABLE", None),
        ({"amount": "1e3"}, "AMOUNT_UNREADABLE", None),  # scientific notation (audit fix)
        ({"amount": "NaN"}, "AMOUNT_UNREADABLE", None),
        ({"amount": "Infinity"}, "AMOUNT_UNREADABLE", None),
        ({"amount": "0"}, "REJECTED_AMOUNT", None),
        ({"amount": "-10.00"}, "REJECTED_AMOUNT", None),
        ({"amount": "12345678900"}, "REJECTED_AMOUNT", None),  # a CPF read as amount: above max
        ({"amount": "12345678000195"}, "REJECTED_AMOUNT", None),  # a CNPJ read as amount
        ({"transaction_date": "07/10/2026"}, "DATE_UNREADABLE", None),
    ],
)
def test_receipts_without_safe_proposal_create_nothing(
    client, alice, evolution, download, storage, vision, overrides, code, message
):
    vision.extract(**overrides)
    record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, code)
    assert not Transaction.objects.exists()
    attachment = ReceiptAttachment.objects.get()
    assert (attachment.status, attachment.transaction, attachment.error_code) == (
        AStatus.PROCESSED,
        None,
        code,
    )
    if message:
        assert reply(evolution) == message


def test_future_date_rejected(client, alice, evolution, download, storage, vision):
    vision.extract(transaction_date=(today_local() + timedelta(days=3)).isoformat())
    record = post_image(client)
    record.refresh_from_db()
    assert record.error_code == "REJECTED_TRANSACTION_DATE"
    assert not Transaction.objects.exists()


def test_invalid_category_falls_back_but_incompatible_subcategory_is_dropped(
    client, alice, catalog, evolution, download, storage, vision
):
    vision.extract(category_id=999999, subcategory_id=catalog["combustivel"].pk)
    post_image(client)
    txn = Transaction.objects.get()
    assert (txn.category, txn.subcategory) == (catalog["transporte"], catalog["combustivel"])

    vision.extract(category_id=catalog["alimentacao"].pk, subcategory_id=catalog["combustivel"].pk)
    post_image(client, message_id="IMG2")
    second = Transaction.objects.exclude(pk=txn.pk).get()
    assert (second.category, second.subcategory) == (catalog["alimentacao"], None)

    vision.extract(category_id=catalog["salario"].pk)  # INCOME category for an expense
    post_image(client, message_id="IMG3")
    third = Transaction.objects.exclude(pk__in=[txn.pk, second.pk]).get()
    assert third.category == catalog["outros_gastos"]


# --- Filters before any download -------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"from_me": True}, "FROM_ME"),
        ({"remote_jid": "120363000000000000@g.us"}, "GROUP"),
        ({"remote_jid": "status@broadcast"}, "BROADCAST"),
        ({"remote_jid": "5521911112222@s.whatsapp.net"}, "UNKNOWN_SENDER"),
    ],
)
def test_ignored_images_are_never_downloaded(
    client, alice, download, storage, vision, kwargs, reason
):
    record = post_image(client, **kwargs)
    assert (record.status, record.ignore_reason) == (Status.IGNORED, reason)
    download.assert_not_called()
    storage.upload.assert_not_called()
    vision.assert_not_called()


# --- Validation and limits ---------------------------------------------------------------------


def test_declared_size_above_limit_rejected_before_download(
    client, alice, evolution, download, storage, vision
):
    record = post_image(client, file_length=5 * 1024 * 1024)
    record.refresh_from_db()
    assert record.error_code == "IMAGE_TOO_LARGE"
    download.assert_not_called()
    assert reply(evolution) == messages.IMAGE_TOO_LARGE


def test_actual_size_above_limit(client, alice, evolution, download, storage, vision):
    download.side_effect = MediaTooLargeError("big")
    record = post_image(client)
    record.refresh_from_db()
    assert record.error_code == "IMAGE_TOO_LARGE"
    storage.upload.assert_not_called()


@pytest.mark.parametrize(
    "data, code",
    [
        (b"fake jpg named receipt.jpg" * 10, "IMAGE_UNSUPPORTED_FORMAT"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 200, "IMAGE_CORRUPTED"),
        (b"%PDF-1.7" + b"\x00" * 200, "IMAGE_UNSUPPORTED_FORMAT"),
    ],
)
def test_invalid_image_never_stored_nor_analysed(
    client, alice, evolution, download, storage, vision, data, code, caplog
):
    download.return_value = DownloadedMedia(data, "image/jpeg")
    record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, code)
    storage.upload.assert_not_called()
    vision.assert_not_called()
    assert not ReceiptAttachment.objects.exists()
    assert "media.rejected" in caplog.text
    assert "message_type=IMAGE" in caplog.text


# --- Failures ------------------------------------------------------------------------------------


def test_download_failure(client, alice, evolution, download, storage, vision):
    download.side_effect = EvolutionHTTPError(400, "Message not found")
    record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, "IMAGE_DOWNLOAD_FAILED")
    storage.upload.assert_not_called()
    assert reply(evolution) == messages.IMAGE_DOWNLOAD_FAILED


@pytest.mark.parametrize(
    "code", ["TIMEOUT", "AUTH", "NOT_FOUND", "CONFLICT", "PROVIDER_ERROR", "REJECTED"]
)
def test_storage_failure_stops_before_vision(
    client, alice, evolution, download, storage, vision, code
):
    storage.upload.side_effect = StorageError(code)
    record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, f"STORAGE_{code}")
    vision.assert_not_called()
    assert not ReceiptAttachment.objects.exists()
    assert not Transaction.objects.exists()


@pytest.mark.parametrize(
    "code", ["TIMEOUT", "RATE_LIMIT", "PROVIDER_ERROR", "INVALID_OUTPUT", "REFUSAL", "AUTH"]
)
def test_vision_failure_keeps_evidence_and_creates_nothing(
    client, alice, evolution, download, storage, vision, code
):
    vision.side_effect = AIError(code)
    record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, f"VISION_{code}")
    attachment = ReceiptAttachment.objects.get()
    assert (attachment.status, attachment.error_code, attachment.transaction) == (
        AStatus.FAILED,
        f"VISION_{code}",
        None,
    )
    storage.delete.assert_not_called()  # evidence is not deleted
    assert not Transaction.objects.exists()


def test_failure_while_committing_rolls_back(client, alice, evolution, download, storage, vision):
    original_save = InboundMessage.save

    def failing_save(self, *args, **kwargs):
        if self.status == Status.PROCESSED:
            raise RuntimeError("crash before commit")
        return original_save(self, *args, **kwargs)

    with mock.patch.object(InboundMessage, "save", failing_save):
        record = post_image(client)
    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert not Transaction.objects.exists()
    assert ReceiptAttachment.objects.get().status == AStatus.FAILED


def test_attachment_persistence_failure_cleans_storage_and_terminates_message(
    client, alice, evolution, download, storage, vision
):
    with mock.patch(
        "apps.whatsapp.image.ReceiptAttachment.objects.create",
        side_effect=IntegrityError,
    ):
        record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, "IMAGE_ATTACHMENT_FAILED")
    storage.delete.assert_called_once()
    vision.assert_not_called()
    assert not Transaction.objects.exists()


# --- Idempotency ---------------------------------------------------------------------------------


def test_duplicate_delivery_one_upload_one_vision_one_transaction(
    client, alice, evolution, download, storage, vision
):
    post_image(client)
    post_image(client)
    assert download.call_count == storage.upload.call_count == vision.call_count == 1
    assert Transaction.objects.count() == ReceiptAttachment.objects.count() == 1


def test_reply_failure_does_not_recreate(client, alice, evolution, download, storage, vision):
    evolution.side_effect = EvolutionHTTPError(500, "down")
    record = post_image(client)
    post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "REPLY_SEND_FAILED")
    assert Transaction.objects.count() == 1
    assert storage.upload.call_count == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_image_reserved_once(make_user):
    make_user(phone_number="+5531999990001")
    message = parse_evolution_event(image_payload()).message
    barrier = threading.Barrier(2)
    results, errors = [], []

    def deliver_once():
        try:
            barrier.wait(timeout=5)
            results.append(receive_message(message))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=deliver_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert errors == []
    assert sorted(r.needs_image_processing for r in results) == [False, True]


def test_attachment_one_per_message_at_database_level(
    client, alice, evolution, download, storage, vision
):
    from django.db import IntegrityError

    record = post_image(client)
    attachment = ReceiptAttachment.objects.get()
    with pytest.raises(IntegrityError):
        ReceiptAttachment.objects.create(
            inbound_message=record,
            storage_bucket="b",
            storage_path="receipts/other.jpg",
            content_type="image/jpeg",
            byte_size=1,
            sha256="0" * 64,
            width=1,
            height=1,
        )
    assert attachment.pk


# --- Privacy ------------------------------------------------------------------------------


def test_no_bytes_base64_or_urls_persisted_or_logged(
    client, alice, evolution, download, storage, vision, caplog
):
    caplog.set_level("INFO")
    vision.extract(description="mercado segredo")
    record = post_image(client)
    rows = json.dumps(
        [
            InboundMessage.objects.filter(pk=record.pk).values().get(),
            ReceiptAttachment.objects.values().get(),
        ],
        default=str,
    )
    for forbidden in ("attacker", "joao-cpf", "/9j/", "base64", "http", "PhoneCam"):
        assert forbidden not in rows
    for forbidden in ("segredo", "/9j/", "test-supabase-secret", "47.90", "5531999990001"):
        assert forbidden not in caplog.text
    assert "image.validated" in caplog.text


def test_family_policies_unchanged_for_images(
    client, alice, maria, evolution, download, storage, vision
):
    """An image always creates for the sender; there is no way to target someone else."""
    post_image(client, remote_jid="5521988887777@s.whatsapp.net")  # sent by maria
    assert Transaction.objects.get().user == maria
    assert TransactionChange.objects.get().user == maria
