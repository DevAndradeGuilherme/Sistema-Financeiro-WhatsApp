"""Manual recovery (apps/whatsapp/recovery.py + inbound_recovery command).

Recovery never reprocesses a message: these tests check that the financial pipeline is not
called again, that the ledger is untouched, that ambiguous states are refused and that
concurrent recoveries act at most once (PostgreSQL row locks, separate connections).
"""

import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.db import connection, connections
from django.utils import timezone

from apps.transactions.models import Transaction, TransactionChange, TransactionType
from apps.transactions.services import create_transaction, undo_last_change
from apps.whatsapp import messages, recovery
from apps.whatsapp.audio import handle_audio_message
from apps.whatsapp.client import EvolutionHTTPError
from apps.whatsapp.models import InboundMessage, ReceiptAttachment
from apps.whatsapp.parsers import parse_evolution_event
from apps.whatsapp.pipeline import handle_ready_message, idempotency_key
from apps.whatsapp.recovery import Action
from common.dates import today_local

from . import payloads
from .replies import card
from .test_pipeline import create_proposal, deliver

pytestmark = pytest.mark.django_db

Status = InboundMessage.Status
PHONE = "+5531999990001"
PROVIDER_ID = "3EB0C431D5A8F0001"
TEXT = "50 reais de mercado"
OLD = timedelta(hours=1)


def make_message(user, *, status=Status.READY, age=OLD, provider_id=PROVIDER_ID, **fields):
    fields.setdefault("message_type", "TEXT")
    fields.setdefault("text", TEXT if status == Status.READY else "")
    record = InboundMessage.objects.create(
        instance="test-instance",
        provider_message_id=provider_id,
        sender_phone=user.phone_number if user else "",
        user=user,
        status=status,
        **fields,
    )
    InboundMessage.objects.filter(pk=record.pk).update(updated_at=timezone.now() - age)
    record.refresh_from_db()
    return record


def committed_create(user, category, **fields):
    """A CREATE committed by the pipeline whose reply failed (PROCESSED + REPLY_SEND_FAILED)."""
    record = make_message(
        user,
        status=Status.PROCESSED,
        intent="CREATE_TRANSACTION",
        error_code="REPLY_SEND_FAILED",
        **fields,
    )
    txn = create_transaction(
        user=user,
        type=TransactionType.EXPENSE,
        amount=Decimal("37.90"),
        category=category,
        transaction_date=today_local(),
        source="TEXT",
        idempotency_key=idempotency_key(record),
    )
    InboundMessage.objects.filter(pk=record.pk).update(transaction=txn)
    record.refresh_from_db()
    return record, txn


def snapshot_state(record):
    record.refresh_from_db()
    return (
        record.status,
        record.error_code,
        record.transaction_id,
        Transaction.objects.count(),
        TransactionChange.objects.count(),
    )


@pytest.fixture
def send():
    with mock.patch("apps.whatsapp.pipeline.EvolutionClient") as cls:
        yield cls.return_value.send_text


@pytest.fixture
def no_financial_pipeline():
    """Any attempt to interpret, transcribe, extract or write to the ledger fails the test."""
    targets = [
        "apps.whatsapp.pipeline.interpret_message",
        "apps.whatsapp.audio.transcribe_audio",
        "apps.whatsapp.image.extract_receipt",
        "apps.whatsapp.actions.create_transaction",
        "apps.whatsapp.actions.update_transaction",
        "apps.whatsapp.actions.undo_last_change",
    ]
    patches = [mock.patch(t, side_effect=AssertionError(f"{t} called")) for t in targets]
    mocks = [p.start() for p in patches]
    yield mocks
    for p in patches:
        p.stop()
    for m in mocks:
        m.assert_not_called()


# --- diagnosis -----------------------------------------------------------------------


def test_stuck_ready_without_effect_can_be_marked_failed(alice):
    diagnosis = recovery.inspect(make_message(alice).pk)
    assert diagnosis.action == Action.MARK_FAILED
    assert diagnosis.reason == "STUCK_WITHOUT_EFFECT"
    assert diagnosis.has_financial_effect is False


def test_recent_message_may_be_in_flight(alice):
    diagnosis = recovery.inspect(make_message(alice, age=timedelta(minutes=2)).pk)
    assert diagnosis.action == Action.WAIT


@pytest.mark.parametrize("status", [Status.READY, Status.RECEIVED])
def test_effect_without_terminal_status_is_ambiguous(alice, catalog, status):
    record = make_message(alice, status=status)
    create_transaction(
        user=alice,
        type=TransactionType.EXPENSE,
        amount=Decimal("10"),
        category=catalog["transporte"],
        source="TEXT",
        idempotency_key=idempotency_key(record),
    )
    diagnosis = recovery.inspect(record.pk)
    assert diagnosis.action == Action.REFUSE
    assert diagnosis.reason == "EFFECT_WITHOUT_TERMINAL_STATUS"
    assert diagnosis.created_transaction is True


def test_ledger_change_by_origin_counts_as_effect(alice, catalog):
    record = make_message(alice)
    create_transaction(
        user=alice,
        type=TransactionType.EXPENSE,
        amount=Decimal("10"),
        category=catalog["transporte"],
        source="TEXT",
    )
    undo_last_change(user=alice, origin=idempotency_key(record))
    diagnosis = recovery.inspect(record.pk)
    assert diagnosis.ledger_changes is True
    assert diagnosis.action == Action.REFUSE


def test_receipt_row_on_stuck_image_is_ambiguous(alice):
    record = make_message(alice, status=Status.RECEIVED, message_type="IMAGE")
    ReceiptAttachment.objects.create(
        inbound_message=record,
        status=ReceiptAttachment.Status.PROCESSED,
        content_type="image/png",
        byte_size=10,
        sha256="0" * 64,
        width=1,
        height=1,
    )
    assert recovery.inspect(record.pk).action == Action.REFUSE


def test_failed_without_effect_needs_nothing(alice):
    record = make_message(alice, status=Status.FAILED, error_code="AI_TIMEOUT")
    assert recovery.inspect(record.pk).action == Action.NONE


def test_failed_with_effect_is_anomalous(alice, catalog):
    record = make_message(alice, status=Status.FAILED, error_code="PIPELINE_ERROR")
    create_transaction(
        user=alice,
        type=TransactionType.EXPENSE,
        amount=Decimal("10"),
        category=catalog["transporte"],
        source="TEXT",
        idempotency_key=idempotency_key(record),
    )
    assert recovery.inspect(record.pk).reason == "EFFECT_ON_NON_PROCESSED"


@pytest.mark.parametrize("intent", ["QUERY_TRANSACTIONS", "UPDATE_TRANSACTION", "UNDO_TRANSACTION"])
def test_reply_of_other_intents_is_not_derivable(alice, intent):
    record = make_message(
        alice, status=Status.PROCESSED, intent=intent, error_code="REPLY_SEND_FAILED"
    )
    diagnosis = recovery.inspect(record.pk)
    assert (diagnosis.action, diagnosis.reason) == (Action.REFUSE, "REPLY_NOT_DERIVABLE")


def test_create_without_linked_transaction_is_not_confirmed(alice):
    record = make_message(
        alice,
        status=Status.PROCESSED,
        intent="CREATE_TRANSACTION",
        error_code="REPLY_SEND_FAILED",
    )
    assert recovery.inspect(record.pk).reason == "EFFECT_NOT_CONFIRMED"


def test_transaction_changed_since_creation_is_not_resent(alice, catalog):
    record, _ = committed_create(alice, catalog["transporte"])
    undo_last_change(user=alice, origin="whatsapp:test-instance:OTHER")
    assert recovery.inspect(record.pk).reason == "TRANSACTION_CHANGED_SINCE"


def test_inactive_user_does_not_get_replies(alice, catalog):
    record, _ = committed_create(alice, catalog["transporte"])
    alice.is_active = False
    alice.save(update_fields=["is_active"])
    assert recovery.inspect(record.pk).reason == "RECIPIENT_UNAVAILABLE"


def test_list_shows_only_messages_needing_attention(alice, catalog):
    stuck = make_message(alice, provider_id="STUCK000001")
    failed = make_message(alice, status=Status.FAILED, provider_id="FAILED00001")
    reply, _ = committed_create(alice, catalog["transporte"], provider_id="REPLY000001")
    make_message(alice, status=Status.PROCESSED, provider_id="DONE0000001")
    make_message(alice, status=Status.IGNORED, provider_id="IGNORED0001")

    listed = {d.message_id: d.action for d in recovery.needs_attention()}
    assert listed == {
        stuck.pk: Action.MARK_FAILED,
        failed.pk: Action.NONE,
        reply.pk: Action.RESEND_REPLY,
    }


# --- dry-run -------------------------------------------------------------------------


def test_dry_run_mark_failed_changes_nothing(alice, send, no_financial_pipeline):
    record = make_message(alice)
    before = snapshot_state(record)

    result = recovery.mark_stuck_failed(record.pk, execute=False)

    assert result.executed is False
    assert result.diagnosis.action == Action.MARK_FAILED
    assert snapshot_state(record) == before
    send.assert_not_called()


def test_dry_run_resend_changes_nothing(alice, catalog, send, no_financial_pipeline):
    record, _ = committed_create(alice, catalog["transporte"])
    before = snapshot_state(record)

    result = recovery.resend_reply(record.pk, execute=False)

    assert result.executed is False
    assert snapshot_state(record) == before
    send.assert_not_called()


def test_command_defaults_to_dry_run(alice, send):
    record = make_message(alice)
    out = StringIO()
    call_command("inbound_recovery", "mark-failed", str(record.pk), stdout=out)
    record.refresh_from_db()
    assert record.status == Status.READY
    assert "DRY-RUN" in out.getvalue()
    send.assert_not_called()


# --- mark-failed ---------------------------------------------------------------------


def test_mark_failed_closes_stuck_message_without_reprocessing(alice, send, no_financial_pipeline):
    record = make_message(alice)

    result = recovery.mark_stuck_failed(record.pk, execute=True)

    record.refresh_from_db()
    assert result.executed is True
    assert (record.status, record.error_code) == (Status.FAILED, "STUCK_READY")
    assert Transaction.objects.count() == 0
    send.assert_called_once_with(PHONE, messages.TECHNICAL_ERROR)


def test_mark_failed_without_reply(alice, send):
    record = make_message(alice)
    recovery.mark_stuck_failed(record.pk, execute=True, notify=False)
    record.refresh_from_db()
    assert record.status == Status.FAILED
    send.assert_not_called()


def test_mark_failed_is_idempotent(alice, send):
    record = make_message(alice)
    recovery.mark_stuck_failed(record.pk, execute=True)
    with pytest.raises(recovery.RecoveryRefused):
        recovery.mark_stuck_failed(record.pk, execute=True)
    assert send.call_count == 1


def test_mark_failed_refuses_recent_and_ambiguous(alice, catalog, send):
    recent = make_message(alice, age=timedelta(minutes=1), provider_id="RECENT00001")
    with pytest.raises(recovery.RecoveryRefused):
        recovery.mark_stuck_failed(recent.pk, execute=True)
    ambiguous = make_message(alice, provider_id="AMBIGUOUS01")
    create_transaction(
        user=alice,
        type=TransactionType.EXPENSE,
        amount=Decimal("10"),
        category=catalog["transporte"],
        source="TEXT",
        idempotency_key=idempotency_key(ambiguous),
    )
    with pytest.raises(recovery.RecoveryRefused):
        recovery.mark_stuck_failed(ambiguous.pk, execute=True)
    recent.refresh_from_db()
    ambiguous.refresh_from_db()
    assert recent.status == ambiguous.status == Status.READY
    send.assert_not_called()


def test_min_age_cannot_be_shorter_than_in_flight_window(alice):
    record = make_message(alice)
    with pytest.raises(ValueError):
        recovery.mark_stuck_failed(record.pk, execute=True, min_age=timedelta(minutes=1))
    with pytest.raises(CommandError):
        call_command("inbound_recovery", "list", "--min-age-minutes", "1", stdout=StringIO())


def test_late_text_worker_is_fenced_after_mark_failed(alice, catalog, ai, send):
    """A worker that was still holding the READY message cannot commit an effect anymore."""
    record = make_message(alice)
    stale = InboundMessage.objects.get(pk=record.pk)  # the worker's in-memory READY copy
    recovery.mark_stuck_failed(record.pk, execute=True, notify=False)

    ai.set(create_proposal(catalog))
    handle_ready_message(stale)

    assert Transaction.objects.count() == 0
    record.refresh_from_db()
    assert record.status == Status.FAILED


def test_late_audio_promotion_is_fenced_after_mark_failed(alice, send):
    record = make_message(alice, status=Status.RECEIVED, message_type="AUDIO")
    recovery.mark_stuck_failed(record.pk, execute=True, notify=False)
    message = parse_evolution_event(payloads.audio()).message
    stale = InboundMessage.objects.get(pk=record.pk)
    stale.status = Status.RECEIVED  # what the in-flight worker still believes
    with (
        mock.patch("apps.whatsapp.audio.EvolutionClient") as client,
        mock.patch("apps.whatsapp.audio.transcribe_audio") as transcribe,
        mock.patch("apps.whatsapp.audio.handle_ready_message") as pipeline,
    ):
        client.return_value.download_audio.return_value = mock.Mock(data=b"OggS" + b"\0" * 64)
        transcribe.return_value = mock.Mock(text=TEXT)
        handle_audio_message(stale, message)
    pipeline.assert_not_called()
    record.refresh_from_db()
    assert (record.status, record.text) == (Status.FAILED, "")


# --- resend-reply --------------------------------------------------------------------


def test_reply_failure_then_resend_does_not_rerun_pipeline(client, alice, catalog, ai, evolution):
    ai.set(create_proposal(catalog))
    evolution.side_effect = EvolutionHTTPError(500, "down")
    record = deliver(client, "10 reais de gasolina")
    assert record.error_code == "REPLY_SEND_FAILED"
    evolution.side_effect = None
    evolution.reset_mock()
    txn = Transaction.objects.get()
    ledger_before = (txn.amount, txn.category_id, txn.transaction_date, txn.family_id)

    result = recovery.resend_reply(record.pk, execute=True)

    assert result.reply_sent is True
    assert ai.call_count == 1  # never interpreted again
    assert Transaction.objects.count() == 1
    assert TransactionChange.objects.count() == 1
    txn.refresh_from_db()
    assert (txn.amount, txn.category_id, txn.transaction_date, txn.family_id) == ledger_before
    phone, text = evolution.call_args.args
    assert phone == PHONE
    assert card(text)["Valor"] == "R$ 10,00"
    assert card(text)["Categoria"] == "Transporte"
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "REPLY_RESENT")


def test_resend_is_at_most_once(alice, catalog, send, no_financial_pipeline):
    record, _ = committed_create(alice, catalog["transporte"])
    recovery.resend_reply(record.pk, execute=True)
    with pytest.raises(recovery.RecoveryRefused) as refused:
        recovery.resend_reply(record.pk, execute=True)
    assert refused.value.diagnosis.action == Action.NONE
    assert send.call_count == 1


def test_resend_failure_keeps_message_recoverable(alice, catalog, send):
    record, _ = committed_create(alice, catalog["transporte"])
    send.side_effect = EvolutionHTTPError(503, "down")

    result = recovery.resend_reply(record.pk, execute=True)

    record.refresh_from_db()
    assert result.reply_sent is False
    assert record.error_code == "REPLY_SEND_FAILED"
    assert recovery.inspect(record.pk).action == Action.RESEND_REPLY


def test_interrupted_resend_is_refused(alice, catalog, send):
    """A claim left behind (process died during the send) means delivery is unknown."""
    record, _ = committed_create(alice, catalog["transporte"])
    InboundMessage.objects.filter(pk=record.pk).update(error_code=recovery.REPLY_RESEND_PENDING)

    with pytest.raises(recovery.RecoveryRefused) as refused:
        recovery.resend_reply(record.pk, execute=True)

    assert refused.value.diagnosis.reason == "RESEND_OUTCOME_UNKNOWN"
    send.assert_not_called()


def test_resend_refused_for_stuck_message(alice, send):
    record = make_message(alice)
    with pytest.raises(recovery.RecoveryRefused):
        recovery.resend_reply(record.pk, execute=True)
    send.assert_not_called()


# --- concurrency (PostgreSQL, separate connections) ---------------------------------


def _race(target, n=2):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def run():
        try:
            barrier.wait(timeout=5)
            results.append(target())
        except recovery.RecoveryRefused as exc:
            results.append(exc)
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run) for _ in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert errors == []
    return results


@pytest.mark.django_db(transaction=True)
def test_concurrent_resends_send_once(alice, catalog, send):
    record, _ = committed_create(alice, catalog["transporte"])

    results = _race(lambda: recovery.resend_reply(record.pk, execute=True))

    executed = [r for r in results if isinstance(r, recovery.Result) and r.executed]
    assert len(executed) == 1
    assert send.call_count == 1
    assert Transaction.objects.count() == 1
    record.refresh_from_db()
    assert record.error_code == "REPLY_RESENT"
    assert connection.vendor == "postgresql"


@pytest.mark.django_db(transaction=True)
def test_concurrent_mark_failed_acts_once(alice, send):
    record = make_message(alice)

    results = _race(lambda: recovery.mark_stuck_failed(record.pk, execute=True))

    executed = [r for r in results if isinstance(r, recovery.Result) and r.executed]
    assert len(executed) == 1
    assert send.call_count == 1
    record.refresh_from_db()
    assert record.status == Status.FAILED


# --- command output ------------------------------------------------------------------


def _output(*args) -> str:
    out = StringIO()
    call_command("inbound_recovery", *args, stdout=out)
    return out.getvalue()


def test_command_output_never_leaks_content(alice, catalog):
    stuck = make_message(alice, provider_id="STUCKSECRET0042")
    created, _ = committed_create(alice, catalog["transporte"], provider_id="CREATEDSECRET77")
    output = (
        _output("list")
        + _output("inspect", str(stuck.pk))
        + _output("inspect", str(created.pk))
        + _output("resend-reply", str(created.pk))
    )

    assert "+55319****0001" in output
    for secret in (PHONE, "5531999990001", TEXT, "STUCKSECRET0042", "CREATEDSECRET77"):
        assert secret not in output
    for amount in ("37,90", "37.90"):
        assert amount not in output
    assert "safe_action: MARK_FAILED" in output
    assert "transaction_created: sim" in output


def test_command_refusal_is_an_error_and_changes_nothing(alice, send):
    record = make_message(alice, age=timedelta(minutes=1))
    with pytest.raises(CommandError, match="RECUSADO"):
        _output("mark-failed", str(record.pk), "--execute")
    record.refresh_from_db()
    assert record.status == Status.READY
    send.assert_not_called()


def test_command_execute_mark_failed(alice, send):
    record = make_message(alice)
    output = _output("mark-failed", str(record.pk), "--execute")
    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert "EXECUTADO" in output


def test_command_rejects_execute_on_read_only_operations(alice):
    record = make_message(alice)
    with pytest.raises(CommandError):
        _output("list", "--execute")
    with pytest.raises(CommandError):
        _output("inspect", str(record.pk), "--execute")
    with pytest.raises(CommandError, match="não encontrada"):
        _output("inspect", "999999")
