"""Receipt image → download → normalize in memory → vision → domain. Nothing is stored.

Mocked boundaries: Evolution download (``image.EvolutionClient``), vision
(``image.extract_receipt``) and the reply (``pipeline.EvolutionClient``). Image validation
(Pillow), Django rules, the financial domain, idempotency and the database are real. Any other
network access (e.g. a Storage upload) fails the test: sockets are blocked.
"""

import base64
import hashlib
import importlib.util
import io
import json
import socket
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.conf import settings
from django.db import IntegrityError, OperationalError, connection, connections
from django.test import override_settings
from django.utils import timezone
from PIL import Image

from apps.ai.contract import ReceiptExtraction, TransactionReference
from apps.ai.service import AIError
from apps.ai.vision import VisionResult
from apps.transactions.models import Transaction, TransactionChange
from apps.whatsapp import messages
from apps.whatsapp.client import DownloadedMedia, EvolutionHTTPError, MediaTooLargeError
from apps.whatsapp.models import InboundMessage, ReceiptAttachment
from apps.whatsapp.parsers import parse_evolution_event
from apps.whatsapp.services import receive_message
from common.dates import today_local

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


@pytest.fixture(autouse=True)
def no_network():
    """Only the mocked boundaries may "talk": any real socket (Storage, CDN...) is a failure."""
    blocked = AssertionError("unexpected network access")
    with (
        mock.patch.object(socket, "create_connection", side_effect=blocked) as create,
        mock.patch.object(socket.socket, "connect", side_effect=blocked) as connect,
    ):
        yield
    create.assert_not_called()
    connect.assert_not_called()


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
    client, alice, catalog, evolution, download, vision
):
    record = post_image(client)

    download.assert_called_once_with("IMG1", max_bytes=1024 * 1024)
    vision.assert_called_once()
    data = vision.call_args.args[0]
    assert data != JPEG  # never the original bytes
    assert vision.call_args.kwargs["content_type"] == "image/jpeg"
    assert b"PhoneCam" not in data and b"GPS" not in data  # EXIF/GPS removed before vision

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
    assert (attachment.storage_bucket, attachment.storage_path) == (None, None)  # not stored
    assert attachment.document_type == "RECEIPT"
    assert (attachment.width, attachment.height, attachment.byte_size) == (400, 600, len(data))
    assert attachment.content_type == "image/jpeg"
    assert attachment.sha256 == hashlib.sha256(data).hexdigest()  # normalized bytes sent to vision
    record.refresh_from_db()
    assert (record.status, record.transaction, record.intent) == (
        Status.PROCESSED,
        txn,
        "CREATE_TRANSACTION",
    )
    text = reply(evolution)
    assert replies.card(text) == replies.expense("47,90", "Alimentação", txn.transaction_date)
    assert "guardado" not in text and "armazenado" not in text  # nothing is kept


def test_storage_is_gone_and_service_role_not_needed(client, alice, evolution, download, vision):
    assert importlib.util.find_spec("common.storage") is None
    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_STORAGE_BUCKET"):
        assert not hasattr(settings, name)
    post_image(client)  # works end to end without any Storage configuration
    assert Transaction.objects.count() == ReceiptAttachment.objects.count() == 1


def test_png_profile_never_reaches_vision(client, alice, evolution, download, vision):
    buffer = io.BytesIO()
    Image.new("RGB", (120, 80)).save(buffer, "PNG", icc_profile=b"synthetic-private-profile-marker")
    download.return_value = DownloadedMedia(buffer.getvalue(), "image/png")
    post_image(client)
    vision.assert_called_once()
    normalized = vision.call_args.args[0]
    with Image.open(io.BytesIO(normalized)) as image:
        assert "icc_profile" not in image.info
    attachment = ReceiptAttachment.objects.get()
    assert attachment.sha256 == hashlib.sha256(normalized).hexdigest()
    assert attachment.byte_size == len(normalized)
    assert attachment.storage_bucket is None and attachment.storage_path is None


def test_pix_received_creates_income(client, alice, catalog, evolution, download, vision):
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
    attachment = ReceiptAttachment.objects.get()
    assert (attachment.document_type, attachment.transaction, attachment.storage_path) == (
        "PAYMENT_PROOF",
        txn,
        None,
    )


def test_pix_paid_creates_expense(client, alice, catalog, evolution, download, vision):
    vision.extract(document_type="PAYMENT_PROOF", transaction_type="EXPENSE", amount="80.00")
    post_image(client)
    txn = Transaction.objects.get()
    assert (txn.type, txn.amount, txn.source) == ("EXPENSE", Decimal("80.00"), "IMAGE")
    assert ReceiptAttachment.objects.get().transaction == txn


def test_ambiguous_pix_creates_nothing_and_asks(client, alice, evolution, download, vision):
    vision.extract(document_type="PAYMENT_PROOF", transaction_type="UNKNOWN", amount="80.00")
    record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "DIRECTION_UNCLEAR")
    assert not Transaction.objects.exists()
    attachment = ReceiptAttachment.objects.get()
    assert (attachment.status, attachment.transaction, attachment.storage_path) == (
        AStatus.PROCESSED,
        None,
        None,
    )
    assert reply(evolution) == messages.RECEIPT_DIRECTION_UNCLEAR


def test_non_financial_image_keeps_only_metadata(client, alice, evolution, download, vision):
    vision.extract(
        is_financial_document=False,
        document_type="OTHER",
        transaction_type="UNKNOWN",
        amount=None,
        currency="UNKNOWN",
    )
    record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code, record.transaction) == (
        Status.PROCESSED,
        "NOT_A_RECEIPT",
        None,
    )
    assert not Transaction.objects.exists()
    attachment = ReceiptAttachment.objects.get()
    assert (attachment.status, attachment.document_type, attachment.error_code) == (
        AStatus.PROCESSED,
        "OTHER",
        "NOT_A_RECEIPT",
    )
    assert (attachment.storage_bucket, attachment.storage_path) == (None, None)
    assert len(attachment.sha256) == 64 and attachment.byte_size > 0


def test_missing_printed_date_uses_message_date(client, alice, evolution, download, vision):
    vision.extract(transaction_date=None)
    record = post_image(client)
    # The fixture's messageTimestamp is 2026-10-07T00:00Z = 2026-10-06 21:00 in São Paulo:
    # the business-timezone date of *receipt* is used (never "today" by accident).
    expected = timezone.localtime(record.provider_timestamp).date()
    assert expected.isoformat() == "2026-10-06"
    assert Transaction.objects.get().transaction_date == expected


def test_undo_by_text_soft_deletes_but_keeps_receipt(
    client, alice, catalog, ai, evolution, download, vision
):
    post_image(client)
    ai.set(interp(intent="UNDO_TRANSACTION", reference=TransactionReference(last=True)))
    deliver(client, "desfaça o último lançamento", "T1")
    txn = Transaction.objects.get()
    assert txn.deleted_at is not None
    attachment = ReceiptAttachment.objects.get()
    assert attachment.transaction == txn  # metadata kept, still linked


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
    client, alice, evolution, download, vision, overrides, code, message
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


def test_future_date_rejected(client, alice, evolution, download, vision):
    vision.extract(transaction_date=(today_local() + timedelta(days=3)).isoformat())
    record = post_image(client)
    record.refresh_from_db()
    assert record.error_code == "REJECTED_TRANSACTION_DATE"
    assert not Transaction.objects.exists()


def test_invalid_category_falls_back_but_incompatible_subcategory_is_dropped(
    client, alice, catalog, evolution, download, vision
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
def test_ignored_images_are_never_downloaded(client, alice, download, vision, kwargs, reason):
    record = post_image(client, **kwargs)
    assert (record.status, record.ignore_reason) == (Status.IGNORED, reason)
    download.assert_not_called()
    vision.assert_not_called()


# --- Validation and limits ---------------------------------------------------------------------


def test_declared_size_above_limit_rejected_before_download(
    client, alice, evolution, download, vision
):
    record = post_image(client, file_length=5 * 1024 * 1024)
    record.refresh_from_db()
    assert record.error_code == "IMAGE_TOO_LARGE"
    download.assert_not_called()
    assert reply(evolution) == messages.IMAGE_TOO_LARGE


def test_actual_size_above_limit(client, alice, evolution, download, vision):
    download.side_effect = MediaTooLargeError("big")
    record = post_image(client)
    record.refresh_from_db()
    assert record.error_code == "IMAGE_TOO_LARGE"
    vision.assert_not_called()
    assert not ReceiptAttachment.objects.exists()


def test_decompression_bomb_rejected_before_vision(client, alice, evolution, download, vision):
    buffer = io.BytesIO()
    Image.new("L", (3000, 3000), 255).save(buffer, "PNG")  # tiny file, 9 Mpx > 4 Mpx limit
    download.return_value = DownloadedMedia(buffer.getvalue(), "image/png")
    record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "IMAGE_TOO_MANY_PIXELS")
    vision.assert_not_called()
    assert not ReceiptAttachment.objects.exists()
    assert not Transaction.objects.exists()


@override_settings(IMAGE_MAX_PIXELS=100_000)
def test_pixel_limit_comes_from_settings(client, alice, evolution, download, vision):
    record = post_image(client)  # 400x600 = 240 000 px
    record.refresh_from_db()
    assert record.error_code == "IMAGE_TOO_MANY_PIXELS"
    vision.assert_not_called()


@pytest.mark.parametrize(
    "data, code",
    [
        (b"fake jpg named receipt.jpg" * 10, "IMAGE_UNSUPPORTED_FORMAT"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 200, "IMAGE_CORRUPTED"),
        (b"%PDF-1.7" + b"\x00" * 200, "IMAGE_UNSUPPORTED_FORMAT"),
    ],
)
def test_invalid_image_never_stored_nor_analysed(
    client, alice, evolution, download, vision, data, code, caplog
):
    download.return_value = DownloadedMedia(data, "image/jpeg")
    record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, code)
    vision.assert_not_called()
    assert not ReceiptAttachment.objects.exists()
    assert "media.rejected" in caplog.text
    assert "message_type=IMAGE" in caplog.text


# --- Failures ------------------------------------------------------------------------------------


def test_download_failure(client, alice, evolution, download, vision):
    download.side_effect = EvolutionHTTPError(400, "Message not found")
    record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, "IMAGE_DOWNLOAD_FAILED")
    vision.assert_not_called()
    assert not ReceiptAttachment.objects.exists()
    assert not Transaction.objects.exists()
    assert reply(evolution) == messages.IMAGE_DOWNLOAD_FAILED


@pytest.mark.parametrize(
    "code", ["TIMEOUT", "RATE_LIMIT", "PROVIDER_ERROR", "INVALID_OUTPUT", "REFUSAL", "AUTH"]
)
def test_vision_failure_records_metadata_and_creates_nothing(
    client, alice, evolution, download, vision, code
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
    assert attachment.storage_path is None
    assert not Transaction.objects.exists()
    assert reply(evolution) == messages.TECHNICAL_ERROR


def test_failure_while_committing_rolls_back(client, alice, evolution, download, vision):
    original_save = InboundMessage.save

    def failing_save(self, *args, **kwargs):
        if self.status == Status.PROCESSED:
            raise RuntimeError("crash before commit")
        return original_save(self, *args, **kwargs)

    with mock.patch.object(InboundMessage, "save", failing_save):
        record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, "IMAGE_PIPELINE_ERROR")
    assert not Transaction.objects.exists()
    attachment = ReceiptAttachment.objects.get()  # diagnosis row written after the rollback
    assert (attachment.status, attachment.error_code) == (AStatus.FAILED, "PIPELINE_ERROR")


def test_attachment_persistence_failure_rolls_back_and_terminates_message(
    client, alice, evolution, download, vision
):
    with mock.patch(
        "apps.whatsapp.image.ReceiptAttachment.objects.create",
        side_effect=IntegrityError,
    ):
        record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, "IMAGE_ATTACHMENT_FAILED")
    vision.assert_called_once()
    assert not Transaction.objects.exists()  # rolled back with the attachment
    assert not ReceiptAttachment.objects.exists()
    assert reply(evolution) == messages.TECHNICAL_ERROR


@pytest.mark.django_db(transaction=True)
def test_database_commit_failure_rolls_back_effect_and_remains_idempotent(
    client, alice, evolution, download, vision
):
    original_commit = connection.commit
    failed = False

    def fail_effect_commit():
        nonlocal failed
        if not failed and ReceiptAttachment.objects.filter(status=AStatus.PROCESSED).exists():
            assert Transaction.objects.count() == TransactionChange.objects.count() == 1
            failed = True
            raise OperationalError("synthetic commit failure")
        return original_commit()

    with mock.patch.object(connection, "commit", side_effect=fail_effect_commit):
        record = post_image(client)
    assert failed
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, "IMAGE_PIPELINE_ERROR")
    assert record.transaction_id is None
    assert not Transaction.objects.exists() and not TransactionChange.objects.exists()
    attachment = ReceiptAttachment.objects.get()
    assert attachment.status == AStatus.FAILED and attachment.transaction_id is None
    post_image(client)
    download.assert_called_once()
    vision.assert_called_once()
    evolution.assert_called_once()


def test_failure_row_not_writable_still_fails_message(client, alice, evolution, download, vision):
    vision.side_effect = AIError("TIMEOUT")
    with mock.patch(
        "apps.whatsapp.image.ReceiptAttachment.objects.create",
        side_effect=IntegrityError,
    ):
        record = post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, "VISION_TIMEOUT")
    assert not ReceiptAttachment.objects.exists()


# --- Idempotency ---------------------------------------------------------------------------------


def test_duplicate_delivery_one_download_one_vision_one_transaction(
    client, alice, evolution, download, vision
):
    post_image(client)
    post_image(client)
    assert download.call_count == vision.call_count == 1
    assert Transaction.objects.count() == ReceiptAttachment.objects.count() == 1


def test_same_image_in_new_message_is_a_new_receipt(client, alice, evolution, download, vision):
    post_image(client, message_id="IMG1")
    post_image(client, message_id="IMG2")  # same bytes, new message: legitimate
    assert Transaction.objects.count() == ReceiptAttachment.objects.count() == 2
    hashes = set(ReceiptAttachment.objects.values_list("sha256", flat=True))
    assert len(hashes) == 1  # the hash audits, it does not deduplicate


def test_reply_failure_does_not_recreate(client, alice, evolution, download, vision):
    evolution.side_effect = EvolutionHTTPError(500, "down")
    record = post_image(client)
    post_image(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "REPLY_SEND_FAILED")
    assert Transaction.objects.count() == ReceiptAttachment.objects.count() == 1
    assert download.call_count == vision.call_count == 1


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


def test_attachment_one_per_message_at_database_level(client, alice, evolution, download, vision):
    from django.db import IntegrityError

    record = post_image(client)
    attachment = ReceiptAttachment.objects.get()
    with pytest.raises(IntegrityError):
        ReceiptAttachment.objects.create(
            inbound_message=record,
            status=AStatus.PROCESSED,
            content_type="image/jpeg",
            byte_size=1,
            sha256="0" * 64,
            width=1,
            height=1,
        )
    assert attachment.pk


def test_legacy_storage_columns_stay_valid_and_opaque(alice):
    """Rows from the Storage era keep their path; a non-opaque path is still refused."""
    legacy = {
        "status": AStatus.PROCESSED,
        "content_type": "image/jpeg",
        "byte_size": 1,
        "sha256": "0" * 64,
        "width": 32,
        "height": 32,
    }

    def message(message_id):
        return InboundMessage.objects.create(
            instance="test-instance", provider_message_id=message_id, message_type="IMAGE"
        )

    old = ReceiptAttachment.objects.create(
        inbound_message=message("OLD"),
        storage_bucket="receipts-bucket",
        storage_path="receipts/2026/10/" + "a" * 32 + ".jpg",
        **legacy,
    )
    assert old.storage_path.startswith("receipts/")
    with pytest.raises(IntegrityError):
        ReceiptAttachment.objects.create(
            inbound_message=message("BAD"), storage_path="joao-cpf.jpg", **legacy
        )


# --- Privacy ------------------------------------------------------------------------------


def test_no_bytes_base64_or_urls_persisted_or_logged(
    client, alice, evolution, download, vision, caplog
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
    image_b64 = base64.b64encode(vision.call_args.args[0]).decode()[:40]
    for forbidden in ("attacker", "joao-cpf", "/9j/", "base64", "http", "PhoneCam", image_b64):
        assert forbidden not in rows
    for forbidden in ("receipts/", "data:image", "segredo"):  # no path, data URL or OCR text
        assert forbidden not in rows
    for forbidden in ("segredo", "/9j/", image_b64, "47.90", "5531999990001"):
        assert forbidden not in caplog.text
    assert "image.validated" in caplog.text


def test_family_policies_unchanged_for_images(client, alice, maria, evolution, download, vision):
    """An image always creates for the sender; there is no way to target someone else."""
    post_image(client, remote_jid="5521988887777@s.whatsapp.net")  # sent by maria
    assert Transaction.objects.get().user == maria
    assert TransactionChange.objects.get().user == maria
