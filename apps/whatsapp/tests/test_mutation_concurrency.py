"""PostgreSQL concurrency of mutations and confirmations (audit P1#4; cases 22, 23, 43).

Real threads, separate connections, real row locks. The model call is mocked with a barrier
so that both messages are interpreted *before* either commits: the window in which the old
code had already chosen a transaction id outside the per-user lock.
"""

import threading
from decimal import Decimal
from unittest import mock

import pytest
from django.db import connections
from django.test import Client

from apps.ai.service import InterpretationResult
from apps.transactions.models import Transaction, TransactionChange
from apps.transactions.services import update_transaction
from apps.whatsapp.models import InboundMessage, PendingAction

from .test_mutation_targets import D7, ref, undo, update
from .test_pipeline import deliver

pytestmark = pytest.mark.django_db(transaction=True)
Action = TransactionChange.Action


@pytest.fixture
def incomes(alice, catalog, make_transaction):
    t = make_transaction
    return {
        "a": t(
            user=alice, type="INCOME", category=catalog["salario"], amount="50", transaction_date=D7
        ),
        "b": t(
            user=alice,
            type="INCOME",
            category=catalog["outras_receitas"],
            amount="70",
            transaction_date=D7,
        ),
    }


@pytest.fixture
def evolution():
    with mock.patch("apps.whatsapp.pipeline.EvolutionClient") as cls:
        yield cls.return_value.send_text


def scripted_ai(proposals: dict, barrier: threading.Barrier | None = None):
    """interpret_message mock: proposal chosen by text; optionally all callers meet first."""

    def interpret(text, context, log_ref=None):
        if barrier is not None:
            barrier.wait(timeout=10)
        return InterpretationResult(proposals[text], model="test-model", duration_ms=1)

    return mock.patch("apps.whatsapp.pipeline.interpret_message", side_effect=interpret)


def run_concurrently(*calls):
    errors = []

    def run(call):
        try:
            call()
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert not any(thread.is_alive() for thread in threads)


def send(text, message_id):
    return lambda: deliver(Client(), text, message_id)


def ask_all(evolution):
    with scripted_ai({"apague todas as entradas": undo(ref(all=True, transaction_type="INCOME"))}):
        deliver(Client(), "apague todas as entradas", "B1")
    assert PendingAction.objects.count() == 1


def test_two_simultaneous_confirmations_delete_at_most_once(alice, incomes, evolution):
    """Case 22: two different "sim" messages at once → the set is deleted at most once."""
    ask_all(evolution)
    run_concurrently(send("sim", "C1"), send("sim", "C2"))

    deletes = TransactionChange.objects.filter(action=Action.DELETE).count()
    assert deletes in (0, 2)  # all or nothing, never twice
    confirmed = InboundMessage.objects.filter(provider_message_id__in=["C1", "C2"], error_code="")
    assert confirmed.count() == (1 if deletes else 0)
    if deletes:
        # Only the message reserved right after the question (lowest pk) may confirm it.
        first = InboundMessage.objects.filter(provider_message_id__in=["C1", "C2"]).order_by("pk")
        assert confirmed.get() == first.first()
    assert not PendingAction.objects.exists()


def test_confirmation_and_concurrent_update_are_serialized(alice, incomes, evolution):
    """Case 23: "sim" × an edit of one candidate: either the edit wins and nothing is deleted,
    or the deletion wins and the edit is refused. Never "deleted with a stale snapshot"."""
    ask_all(evolution)
    outcome = {}

    def edit():
        try:
            update_transaction(user=alice, transaction_id=incomes["a"].pk, amount="60")
            outcome["edit"] = "applied"
        except Exception as exc:
            outcome["edit"] = type(exc).__name__

    run_concurrently(send("sim", "C1"), edit)

    incomes["a"].refresh_from_db()
    record = InboundMessage.objects.get(provider_message_id="C1")
    if record.error_code == "":
        assert outcome["edit"] == "ValidationError"  # already deleted
        assert incomes["a"].deleted_at is not None
        assert incomes["a"].amount == Decimal("50.00")
        assert Transaction.objects.deleted().count() == 2
    else:
        assert record.error_code == "CONFIRMATION_STALE"
        assert outcome["edit"] == "applied"
        assert incomes["a"].amount == Decimal("60.00")
        assert not Transaction.objects.deleted().exists()


def test_simultaneous_updates_of_the_same_reference_apply_once(alice, incomes, evolution):
    """Case 43: both messages are interpreted before either commits ("a entrada de 50 era 40"
    and "... era 30"). Selection runs under the sender's lock, so the second one no longer
    finds an income of 50: one UPDATE, never a second edit of a transaction that changed."""
    proposals = {
        "a entrada de 50 era 40": update(ref(transaction_type="INCOME", amount="50"), amount="40"),
        "a entrada de 50 era 30": update(ref(transaction_type="INCOME", amount="50"), amount="30"),
    }
    with scripted_ai(proposals, threading.Barrier(2)):
        run_concurrently(send("a entrada de 50 era 40", "U1"), send("a entrada de 50 era 30", "U2"))

    assert TransactionChange.objects.filter(action=Action.UPDATE).count() == 1
    codes = sorted(
        InboundMessage.objects.filter(provider_message_id__in=["U1", "U2"]).values_list(
            "error_code", flat=True
        )
    )
    assert codes == ["", "NOT_FOUND"]
    incomes["a"].refresh_from_db()
    assert incomes["a"].amount in (Decimal("40.00"), Decimal("30.00"))


def test_simultaneous_deletes_of_the_same_reference_apply_once(alice, incomes, evolution):
    proposals = {
        "apague a entrada de 50": undo(ref(transaction_type="INCOME", amount="50")),
        "remova a entrada de 50": undo(ref(transaction_type="INCOME", amount="50")),
    }
    with scripted_ai(proposals, threading.Barrier(2)):
        run_concurrently(send("apague a entrada de 50", "D1"), send("remova a entrada de 50", "D2"))
    assert TransactionChange.objects.filter(action=Action.DELETE).count() == 1
    assert Transaction.objects.deleted().get() == incomes["a"]


def test_two_simultaneous_bulk_requests_then_yes(
    alice, catalog, incomes, evolution, make_transaction
):
    """Two bulk questions at once, then "sim": at most the set of ONE question is deleted —
    the one asked right before "sim" — never a mix and never the superseded one."""
    expense = make_transaction(
        user=alice, category=catalog["alimentacao"], amount="9", transaction_date=D7
    )
    proposals = {
        "apague todas as entradas": undo(ref(all=True, transaction_type="INCOME")),
        "apague todos os gastos": undo(ref(all=True, transaction_type="EXPENSE")),
    }
    with scripted_ai(proposals, threading.Barrier(2)):
        run_concurrently(
            send("apague todas as entradas", "B1"), send("apague todos os gastos", "B2")
        )
    deliver(Client(), "sim", "C1")

    deleted = set(Transaction.objects.deleted().values_list("pk", flat=True))
    by_pk = sorted(
        InboundMessage.objects.filter(provider_message_id__in=["B1", "B2"]), key=lambda m: m.pk
    )
    latest_question = by_pk[-1]
    allowed = {
        "B1": {incomes["a"].pk, incomes["b"].pk},
        "B2": {expense.pk},
    }
    assert deleted in (set(), allowed[latest_question.provider_message_id])
    assert not PendingAction.objects.exists()


def test_duplicate_confirmation_delivered_concurrently_runs_once(alice, incomes, evolution):
    """Case 42 under concurrency: the same provider id twice at once."""
    ask_all(evolution)
    run_concurrently(send("sim", "C1"), send("sim", "C1"))
    assert TransactionChange.objects.filter(action=Action.DELETE).count() == 2
    assert InboundMessage.objects.filter(provider_message_id="C1").count() == 1
