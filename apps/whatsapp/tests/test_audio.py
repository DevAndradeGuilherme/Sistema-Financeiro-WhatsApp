"""V1.3: audio → download → transcription → the existing textual pipeline.

Boundaries mocked: Evolution download (``audio.EvolutionClient``), transcription
(``audio.transcribe_audio``), interpretation (``pipeline.interpret_message``) and the reply
(``pipeline.EvolutionClient``). The financial domain, authorization and context are real.
"""

import json
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.db import connections
from django.test import override_settings

from apps.ai.contract import TransactionReference, TransactionUpdates
from apps.ai.service import AIError
from apps.ai.transcription import TranscriptionResult
from apps.families.services import add_family_member, create_family
from apps.transactions.models import Transaction, TransactionChange
from apps.whatsapp import messages
from apps.whatsapp.client import (
    DownloadedMedia,
    EvolutionHTTPError,
    EvolutionResponseError,
    EvolutionTimeoutError,
    MediaTooLargeError,
)
from apps.whatsapp.models import ConversationContext, InboundMessage
from apps.whatsapp.parsers import parse_evolution_event
from apps.whatsapp.services import receive_message
from common.dates import today_local

from . import payloads, replies
from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db

URL = "/api/v1/webhooks/evolution/"
SECRET = "test-webhook-secret"
OGG = b"OggS" + b"\x00" * 300
Status = InboundMessage.Status
MARIA_JID = "5521988887777@s.whatsapp.net"


def audio_payload(message_id="AUD1", seconds=4, file_length=None, **kwargs):
    audio = {
        "mimetype": "audio/ogg; codecs=opus",
        "seconds": seconds,
        "ptt": True,
        "url": "https://attacker.example/evil.ogg",
        "fileName": "../../x.ogg",
    }
    if file_length is not None:
        audio["fileLength"] = file_length
    return payloads.upsert(
        message_id=message_id,
        message_type="audioMessage",
        message={"audioMessage": audio},
        **kwargs,
    )


def post_audio(client, **kwargs):
    response = client.post(
        URL,
        data=json.dumps(audio_payload(**kwargs)),
        content_type="application/json",
        headers={"X-Webhook-Secret": SECRET},
    )
    assert response.status_code == 200
    return InboundMessage.objects.get(provider_message_id=kwargs.get("message_id", "AUD1"))


@pytest.fixture
def download():
    with mock.patch("apps.whatsapp.audio.EvolutionClient") as cls:
        cls.return_value.download_audio.return_value = DownloadedMedia(OGG, "audio/ogg")
        yield cls.return_value.download_audio


@pytest.fixture
def transcribe():
    with mock.patch("apps.whatsapp.audio.transcribe_audio") as fn:
        fn.say = lambda text: setattr(
            fn,
            "return_value",
            TranscriptionResult(text=text, model="test-transcribe", duration_ms=5),
        )
        fn.say("gastei dez reais de gasolina")
        yield fn


def reply(evolution):
    return evolution.call_args.args[1]


def gas(catalog, amount="10"):
    return interp(
        intent="CREATE_TRANSACTION",
        transaction_type="EXPENSE",
        amount=amount,
        description="gasolina",
        category_id=catalog["transporte"].pk,
        subcategory_id=catalog["combustivel"].pk,
    )


# --- Happy paths: same textual pipeline -------------------------------------------------------


def test_audio_create_expense(client, alice, catalog, ai, evolution, download, transcribe):
    ai.set(gas(catalog))
    record = post_audio(client)

    download.assert_called_once_with("AUD1", max_bytes=1024 * 1024)
    kwargs = transcribe.call_args.kwargs
    assert transcribe.call_args.args[0] == OGG
    assert (kwargs["filename"], kwargs["content_type"]) == ("audio.ogg", "audio/ogg")
    ai.assert_called_once()
    assert ai.call_args.args[0] == "gastei dez reais de gasolina"  # transcript → same pipeline

    txn = Transaction.objects.get()
    assert txn.source == "AUDIO"  # audit trail of the input channel; same rules as text
    assert (txn.user, txn.amount, txn.category, txn.subcategory) == (
        alice,
        Decimal("10.00"),
        catalog["transporte"],
        catalog["combustivel"],
    )
    record.refresh_from_db()
    assert (record.status, record.intent, record.transaction, record.text) == (
        Status.PROCESSED,
        "CREATE_TRANSACTION",
        txn,
        "gastei dez reais de gasolina",
    )
    assert replies.card(reply(evolution)) == replies.expense(
        "10,00", "Transporte", today_local(), "Combustível"
    )


def test_audio_income_and_query(client, alice, catalog, ai, evolution, download, transcribe):
    transcribe.say("recebi vinte e cinco reais")
    ai.set(interp(intent="CREATE_TRANSACTION", transaction_type="INCOME", amount="25"))
    post_audio(client, message_id="A1")
    assert Transaction.objects.get().type == "INCOME"

    transcribe.say("quanto gastei de transporte esse mês?")
    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            category_id=catalog["transporte"].pk,
        )
    )
    post_audio(client, message_id="A2")
    assert reply(evolution).startswith("Você não teve gastos com Transporte")
    assert Transaction.objects.count() == 1


def test_audio_update_and_undo(client, alice, catalog, ai, evolution, download, transcribe):
    ai.set(gas(catalog))
    post_audio(client, message_id="A1")
    txn = Transaction.objects.get()

    transcribe.say("muda meu último gasto para quinze reais")
    ai.set(
        interp(
            intent="UPDATE_TRANSACTION",
            reference=TransactionReference(last=True),
            updates=TransactionUpdates(amount="15"),
        )
    )
    post_audio(client, message_id="A2")
    txn.refresh_from_db()
    assert txn.amount == Decimal("15.00")

    transcribe.say("desfazer")
    ai.set(interp(intent="UNDO_TRANSACTION"))
    post_audio(client, message_id="A3")
    txn.refresh_from_db()
    assert txn.amount == Decimal("10.00")
    assert TransactionChange.objects.filter(action="UNDO").count() == 1


def test_audio_family_query_follows_v12_policies(
    client, alice, maria, catalog, ai, evolution, download, transcribe, make_transaction
):
    family = create_family(name="Família", admin=alice)
    add_family_member(family=family, user=maria)
    make_transaction(
        user=maria, category=catalog["alimentacao"], amount="20", transaction_date=today_local()
    )
    transcribe.say("quanto minha família gastou esse mês?")
    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            scope="FAMILY",
        )
    )
    post_audio(client, message_id="A1")  # alice = ADMIN
    assert reply(evolution).startswith("Sua família gastou R$ 20,00")

    record = post_audio(client, message_id="A2", remote_jid=MARIA_JID)  # maria = MEMBER
    record.refresh_from_db()
    assert record.error_code == "ADMIN_ONLY"


def test_context_text_then_audio_and_audio_then_text(
    client, alice, catalog, ai, evolution, download, transcribe
):
    d = today_local()
    prev_end = d.replace(day=1) - timedelta(days=1)
    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            category_id=catalog["transporte"].pk,
        )
    )
    deliver(client, "quanto gastei de transporte esse mês?", "T1")

    transcribe.say("e mês passado?")
    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            follow_up=True,
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            category_id=catalog["transporte"].pk,
            start_date=prev_end.replace(day=1).isoformat(),
            end_date=prev_end.isoformat(),
        )
    )
    post_audio(client, message_id="A1")
    assert ai.call_args.args[1].previous_query["category_id"] == catalog["transporte"].pk
    assert "com Transporte" in reply(evolution)

    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            follow_up=True,
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            category_id=catalog["alimentacao"].pk,
        )
    )
    deliver(client, "e alimentação?", "T2")
    assert ai.call_args.args[1].previous_query["start_date"] == prev_end.replace(day=1).isoformat()
    assert "com Alimentação" in reply(evolution)
    context = ConversationContext.objects.get(user=alice).last_query
    assert "OggS" not in json.dumps(context)


# --- Filters before any download ------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"from_me": True}, "FROM_ME"),
        ({"remote_jid": "120363000000000000@g.us"}, "GROUP"),
        ({"remote_jid": "status@broadcast"}, "BROADCAST"),
        ({"remote_jid": "5521911112222@s.whatsapp.net"}, "UNKNOWN_SENDER"),
    ],
)
def test_ignored_audio_is_never_downloaded(client, alice, ai, download, transcribe, kwargs, reason):
    record = post_audio(client, **kwargs)
    assert (record.status, record.ignore_reason) == (Status.IGNORED, reason)
    download.assert_not_called()
    transcribe.assert_not_called()
    ai.assert_not_called()


# --- Idempotency ----------------------------------------------------------------------------


def test_duplicate_audio_downloads_and_transcribes_once(
    client, alice, catalog, ai, evolution, download, transcribe
):
    ai.set(gas(catalog))
    post_audio(client)
    post_audio(client)
    assert download.call_count == transcribe.call_count == ai.call_count == 1
    assert Transaction.objects.count() == 1


def test_reply_failure_after_effect_does_not_repeat(
    client, alice, catalog, ai, evolution, download, transcribe
):
    ai.set(gas(catalog))
    evolution.side_effect = EvolutionHTTPError(500, "down")
    record = post_audio(client)
    post_audio(client)  # redelivery after the financial effect
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "REPLY_SEND_FAILED")
    assert download.call_count == transcribe.call_count == 1
    assert Transaction.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_audio_reserved_once(make_user):
    make_user(phone_number="+5531999990001")
    message = parse_evolution_event(audio_payload()).message
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
    assert sorted(r.needs_transcription for r in results) == [False, True]  # only one proceeds
    assert InboundMessage.objects.count() == 1


# --- Failures: no financial effect ---------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        EvolutionHTTPError(400, "Message not found"),
        EvolutionTimeoutError("t"),
        EvolutionResponseError("Empty media."),
        EvolutionResponseError("Invalid base64 media."),
    ],
)
def test_download_failure(client, alice, ai, evolution, download, transcribe, error):
    download.side_effect = error
    record = post_audio(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, "AUDIO_DOWNLOAD_FAILED")
    transcribe.assert_not_called()
    ai.assert_not_called()
    assert not Transaction.objects.exists()
    assert reply(evolution) == messages.AUDIO_DOWNLOAD_FAILED


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seconds": 301},
        {"file_length": 2 * 1024 * 1024},
        {"file_length": {"low": 2_000_000, "high": 0}},
    ],
)
def test_declared_limits_reject_before_download(
    client, alice, ai, evolution, download, transcribe, kwargs
):
    record = post_audio(client, **kwargs)
    record.refresh_from_db()
    assert record.status == Status.PROCESSED
    assert record.error_code in ("AUDIO_TOO_LONG", "AUDIO_TOO_LARGE")
    download.assert_not_called()
    assert reply(evolution) == messages.AUDIO_TOO_LARGE


def test_actual_size_limit(client, alice, ai, evolution, download, transcribe):
    download.side_effect = MediaTooLargeError("too big")
    record = post_audio(client, seconds=10)
    record.refresh_from_db()
    assert record.error_code == "AUDIO_TOO_LARGE"
    transcribe.assert_not_called()


@override_settings(AUDIO_MAX_SECONDS=10)
def test_duration_exactly_at_limit_is_accepted(
    client, alice, catalog, ai, evolution, download, transcribe
):
    ai.set(gas(catalog))
    post_audio(client, seconds=10)
    assert Transaction.objects.count() == 1


def test_unsupported_format(client, alice, ai, evolution, download, transcribe):
    download.return_value = DownloadedMedia(b"%PDF-1.7" + b"\x00" * 100, "audio/ogg")
    record = post_audio(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "AUDIO_UNSUPPORTED_FORMAT")
    transcribe.assert_not_called()


@pytest.mark.parametrize(
    "code",
    [
        "TIMEOUT",
        "AUTH",
        "PERMISSION",
        "RATE_LIMIT",
        "BAD_REQUEST",
        "PROVIDER_ERROR",
        "MODEL_UNAVAILABLE",
        "INVALID_OUTPUT",
    ],
)
def test_transcription_failure(client, alice, ai, evolution, download, transcribe, code):
    transcribe.side_effect = AIError(code)
    record = post_audio(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, f"TRANSCRIPTION_{code}")
    ai.assert_not_called()
    assert not Transaction.objects.exists()
    assert reply(evolution) == messages.TECHNICAL_ERROR


@pytest.mark.parametrize("code", ["EMPTY_TRANSCRIPT", "EMPTY_AUDIO"])
def test_empty_transcript(client, alice, ai, evolution, download, transcribe, code):
    transcribe.side_effect = AIError(code)
    record = post_audio(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, code)
    ai.assert_not_called()
    assert reply(evolution) == messages.AUDIO_NOT_UNDERSTOOD


def test_transcript_too_long(client, alice, ai, evolution, download, transcribe):
    transcribe.say("a" * 5000)
    record = post_audio(client)
    record.refresh_from_db()
    assert record.error_code == "TRANSCRIPT_TOO_LONG"
    ai.assert_not_called()


def test_ai_failure_after_transcription_creates_nothing(
    client, alice, ai, evolution, download, transcribe
):
    ai.side_effect = AIError("TIMEOUT")
    record = post_audio(client)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.FAILED, "AI_TIMEOUT")
    assert not Transaction.objects.exists()


# --- Privacy ----------------------------------------------------------------------------------


def test_no_media_or_external_names_persisted_or_logged(
    client, alice, catalog, ai, evolution, download, transcribe, caplog
):
    caplog.set_level("INFO")
    transcribe.say("gastei dez reais de gasolina segredo")
    ai.set(gas(catalog))
    record = post_audio(client)
    stored = json.dumps(InboundMessage.objects.filter(pk=record.pk).values().get(), default=str)
    assert "OggS" not in stored and "attacker" not in stored and "x.ogg" not in stored
    assert "segredo" not in caplog.text
    assert "OggS" not in caplog.text
    assert "test-openai-api-key" not in caplog.text and "test-evolution-api-key" not in caplog.text
    assert transcribe.call_args.kwargs["filename"] == "audio.ogg"  # never the sender's filename
