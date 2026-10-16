"""V1.1 through the pipeline: update, undo, follow-up context, advanced queries, security.

The AI is mocked with the proposal the model is expected to return; Django must select,
validate, compute and answer on its own.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.ai.contract import TransactionReference, TransactionUpdates, parse_output
from apps.transactions.models import Transaction, TransactionChange
from apps.whatsapp import messages
from apps.whatsapp.client import EvolutionHTTPError
from apps.whatsapp.models import ConversationContext, InboundMessage
from common.dates import today_local

from .replies import rows
from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db
Status = InboundMessage.Status
Action = TransactionChange.Action


def today():
    return today_local()


def last_reply(evolution):
    return evolution.call_args.args[1]


def gas_proposal(catalog, amount="10"):
    return interp(
        intent="CREATE_TRANSACTION",
        transaction_type="EXPENSE",
        amount=amount,
        description="gasolina",
        category_id=catalog["transporte"].pk,
        subcategory_id=catalog["combustivel"].pk,
    )


def update(reference=None, **updates):
    return interp(
        intent="UPDATE_TRANSACTION",
        reference=reference,
        updates=TransactionUpdates(**updates),
    )


UNDO = interp(intent="UNDO_TRANSACTION")
LAST = TransactionReference(last=True)


# --- UPDATE -------------------------------------------------------------------------------


def test_update_last_transaction_amount(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais de gasolina", "M1")
    txn = Transaction.objects.get()

    ai.set(update(LAST, amount="15"))
    record = deliver(client, "muda meu último gasto para 15 reais", "M2")

    txn.refresh_from_db()
    assert txn.amount == Decimal("15.00")
    assert Transaction.objects.count() == 1  # same transaction, not a new one
    record.refresh_from_db()
    assert (record.status, record.intent, record.transaction) == (
        Status.PROCESSED,
        "UPDATE_TRANSACTION",
        txn,
    )
    assert last_reply(evolution) == "Pronto, alterei esse gasto para R$ 15,00."
    change = TransactionChange.objects.get(action=Action.UPDATE)
    assert change.origin == "whatsapp:test-instance:M2"


def test_update_without_reference_uses_last(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais de gasolina", "M1")
    ai.set(update(None, amount="12"))
    deliver(client, "muda para 12", "M2")
    assert Transaction.objects.get().amount == Decimal("12.00")


def test_update_by_category_reference(client, alice, catalog, ai, evolution, make_transaction):
    gas = make_transaction(
        user=alice,
        category=catalog["transporte"],
        subcategory=catalog["combustivel"],
        amount="50",
        transaction_date=today(),
    )
    market = make_transaction(
        user=alice,
        category=catalog["alimentacao"],
        amount="50",
        transaction_date=today(),
        description="mercado",
    )
    ai.set(update(TransactionReference(subcategory_id=catalog["combustivel"].pk), amount="20"))
    deliver(client, "aquele gasto de gasolina era 20 reais")
    gas.refresh_from_db()
    market.refresh_from_db()
    assert (gas.amount, market.amount) == (Decimal("20.00"), Decimal("50.00"))


def test_update_by_amount_and_description(client, alice, catalog, ai, evolution, make_transaction):
    market = make_transaction(
        user=alice,
        category=catalog["alimentacao"],
        amount="50",
        transaction_date=today(),
        description="mercado",
    )
    ai.set(update(TransactionReference(amount="50", description="mercado"), amount="45"))
    deliver(client, "o gasto de 50 no mercado era 45")
    market.refresh_from_db()
    assert market.amount == Decimal("45.00")


def test_update_by_date_reference(client, alice, catalog, ai, evolution, make_transaction):
    yesterday = today() - timedelta(days=1)
    txn = make_transaction(
        user=alice, category=catalog["transporte"], amount="30", transaction_date=yesterday
    )
    make_transaction(
        user=alice, category=catalog["transporte"], amount="30", transaction_date=today()
    )
    ai.set(update(TransactionReference(transaction_date=yesterday.isoformat()), amount="32"))
    deliver(client, "corrige o gasto de ontem para 32 reais")
    txn.refresh_from_db()
    assert txn.amount == Decimal("32.00")


def test_ambiguous_reference_changes_nothing(
    client, alice, catalog, ai, evolution, make_transaction
):
    for amount in ("40", "60"):
        make_transaction(
            user=alice,
            category=catalog["transporte"],
            description="gasolina",
            subcategory=catalog["combustivel"],
            amount=amount,
            transaction_date=today(),
        )
    ai.set(
        update(
            TransactionReference(
                subcategory_id=catalog["combustivel"].pk,
                description="gasolina",
                transaction_type="EXPENSE",
            ),
            amount="20",
        )
    )
    record = deliver(client, "aquele gasto de gasolina era 20 reais")
    assert sorted(t.amount for t in Transaction.objects.all()) == [
        Decimal("40.00"),
        Decimal("60.00"),
    ]
    record.refresh_from_db()
    assert record.error_code == "AMBIGUOUS_REFERENCE"
    reply = last_reply(evolution)
    assert reply.startswith("Encontrei mais de uma transação correspondente. Qual delas")
    assert "Gasto de R$ 40,00" in reply and "Gasto de R$ 60,00" in reply
    assert "o valor, a data ou a descrição" in reply
    assert not TransactionChange.objects.filter(action=Action.UPDATE).exists()


@pytest.mark.parametrize(
    "updates, code",
    [
        ({"amount": "0"}, "REJECTED_AMOUNT"),
        ({"amount": "-5"}, "REJECTED_AMOUNT"),
        ({"amount": "abc"}, "INVALID_AMOUNT"),
        ({"transaction_date": "2999-01-01"}, "REJECTED_TRANSACTION_DATE"),
        ({"transaction_date": "ontem"}, "INVALID_DATE"),
        ({"category_id": 999999}, "CATEGORY_NOT_FOUND"),
        ({}, "NOTHING_TO_UPDATE"),
    ],
)
def test_invalid_updates_change_nothing(client, alice, catalog, ai, evolution, updates, code):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais de gasolina", "M1")
    ai.set(update(LAST, **updates))
    record = deliver(client, "muda o último gasto", "M2")
    record.refresh_from_db()
    assert record.error_code == code
    assert Transaction.objects.get().amount == Decimal("10.00")


def test_update_incompatible_category_rejected(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais de gasolina", "M1")
    ai.set(update(LAST, category_id=catalog["salario"].pk))
    record = deliver(client, "muda para salário", "M2")
    record.refresh_from_db()
    assert record.error_code == "REJECTED_CATEGORY"
    assert Transaction.objects.get().category == catalog["transporte"]


def test_update_category_and_subcategory(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais de gasolina", "M1")
    ai.set(update(LAST, subcategory_id=catalog["uber"].pk))
    deliver(client, "na verdade foi uber", "M2")
    txn = Transaction.objects.get()
    assert (txn.category, txn.subcategory) == (catalog["transporte"], catalog["uber"])
    assert last_reply(evolution) == (
        "Pronto, alterei esse gasto: mudei a categoria para Transporte · Uber."
    )

    ai.set(update(LAST, category_id=catalog["alimentacao"].pk, description="lanche"))
    deliver(client, "era alimentação, lanche", "M3")
    txn.refresh_from_db()
    assert (txn.category, txn.subcategory, txn.description) == (
        catalog["alimentacao"],
        None,
        "lanche",
    )


def test_update_date(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais de gasolina", "M1")
    yesterday = today() - timedelta(days=1)
    ai.set(update(LAST, transaction_date=yesterday.isoformat()))
    deliver(client, "foi ontem", "M2")
    assert Transaction.objects.get().transaction_date == yesterday


def test_update_duplicate_delivery_applies_once(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais de gasolina", "M1")
    ai.set(update(LAST, amount="15"))
    deliver(client, "muda para 15", "M2")
    deliver(client, "muda para 15", "M2")
    assert TransactionChange.objects.filter(action=Action.UPDATE).count() == 1
    assert ai.call_count == 2  # one per distinct message


def test_update_reply_failure_does_not_repeat(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais de gasolina", "M1")
    ai.set(update(LAST, amount="15"))
    evolution.side_effect = EvolutionHTTPError(500, "down")
    record = deliver(client, "muda para 15", "M2")
    deliver(client, "muda para 15", "M2")
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "REPLY_SEND_FAILED")
    assert TransactionChange.objects.filter(action=Action.UPDATE).count() == 1


def test_update_and_processed_are_atomic(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais de gasolina", "M1")
    ai.set(update(LAST, amount="15"))
    original_save = InboundMessage.save

    def failing_save(self, *args, **kwargs):
        if self.status == Status.PROCESSED:
            raise RuntimeError("crash before commit")
        return original_save(self, *args, **kwargs)

    with mock.patch.object(InboundMessage, "save", failing_save):
        record = deliver(client, "muda para 15", "M2")
    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert Transaction.objects.get().amount == Decimal("10.00")
    assert not TransactionChange.objects.filter(action=Action.UPDATE).exists()


# --- UNDO ---------------------------------------------------------------------------------


def test_create_update_undo_undo(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais de gasolina", "M1")
    ai.set(update(LAST, amount="15"))
    deliver(client, "muda para 15", "M2")
    txn = Transaction.objects.get()

    ai.set(UNDO)
    record = deliver(client, "desfaz a última alteração", "M3")
    txn.refresh_from_db()
    assert (txn.amount, txn.deleted_at) == (Decimal("10.00"), None)
    assert last_reply(evolution) == (
        "Pronto, desfiz a última alteração. "
        "O gasto voltou para R$ 10,00 em Transporte · Combustível."
    )
    record.refresh_from_db()
    assert (record.intent, record.transaction) == ("UNDO_TRANSACTION", txn)

    deliver(client, "desfaz a última alteração", "M4")
    txn.refresh_from_db()
    assert txn.deleted_at is not None
    assert last_reply(evolution) == (
        "Pronto, removi o último lançamento (R$ 10,00 em Transporte · Combustível)."
    )

    record = deliver(client, "desfaz a última alteração", "M5")
    record.refresh_from_db()
    assert record.error_code == "NOTHING_TO_UNDO"
    assert last_reply(evolution) == "Não encontrei nenhuma ação recente para desfazer."


def test_undo_duplicate_delivery_undoes_once(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog, "1"))
    deliver(client, "1 real de gasolina", "M1")
    ai.set(gas_proposal(catalog, "2"))
    deliver(client, "2 reais de gasolina", "M2")
    ai.set(UNDO)
    deliver(client, "desfaz a última alteração", "M3")
    deliver(client, "desfaz a última alteração", "M3")  # redelivery
    assert Transaction.objects.active().count() == 1
    assert TransactionChange.objects.filter(action=Action.UNDO).count() == 1


def test_undo_reply_failure_does_not_repeat(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais", "M1")
    ai.set(UNDO)
    evolution.side_effect = EvolutionHTTPError(500, "down")
    record = deliver(client, "desfaz a última alteração", "M2")
    deliver(client, "desfaz a última alteração", "M2")
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "REPLY_SEND_FAILED")
    assert TransactionChange.objects.filter(action=Action.UNDO).count() == 1


def test_undo_and_processed_are_atomic(client, alice, catalog, ai, evolution):
    ai.set(gas_proposal(catalog))
    deliver(client, "10 reais", "M1")
    ai.set(UNDO)
    original_save = InboundMessage.save

    def failing_save(self, *args, **kwargs):
        if self.status == Status.PROCESSED:
            raise RuntimeError("crash before commit")
        return original_save(self, *args, **kwargs)

    with mock.patch.object(InboundMessage, "save", failing_save):
        deliver(client, "desfazer", "M2")
    assert Transaction.objects.get().deleted_at is None
    assert not TransactionChange.objects.filter(action=Action.UNDO).exists()


# --- Security -----------------------------------------------------------------------------


def test_cannot_edit_other_users_transaction(
    client, alice, maria, catalog, ai, evolution, make_transaction
):
    theirs = make_transaction(
        user=maria,
        category=catalog["alimentacao"],
        amount="50",
        transaction_date=today(),
        description="maria",
    )
    ai.set(update(TransactionReference(description="maria"), amount="1"))
    record = deliver(client, "edita o gasto da Maria para 1 real")
    theirs.refresh_from_db()
    assert theirs.amount == Decimal("50.00")
    record.refresh_from_db()
    assert record.error_code == "NOT_FOUND"


def test_cannot_edit_other_users_last_transaction(
    client, alice, maria, catalog, ai, evolution, make_transaction
):
    theirs = make_transaction(
        user=maria, category=catalog["alimentacao"], amount="50", transaction_date=today()
    )
    ai.set(update(LAST, amount="1"))
    deliver(client, "muda o último gasto para 1")
    theirs.refresh_from_db()
    assert theirs.amount == Decimal("50.00")


def test_cannot_undo_other_users_action(
    client, alice, maria, catalog, ai, evolution, make_transaction
):
    theirs = make_transaction(user=maria, category=catalog["alimentacao"], amount="50")
    ai.set(UNDO)
    record = deliver(client, "desfaz o gasto do João")
    theirs.refresh_from_db()
    assert theirs.deleted_at is None
    record.refresh_from_db()
    # "gasto" alone identifies nothing (and only the sender's own data is ever searched).
    assert record.error_code == "TARGET_UNCLEAR"


def test_model_cannot_supply_transaction_id():
    """The contract has no transaction id; extra fields are rejected."""
    data = {
        "intent": "UPDATE_TRANSACTION",
        "follow_up": False,
        "transaction_type": None,
        "amount": None,
        "description": None,
        "category_id": None,
        "subcategory_id": None,
        "transaction_date": None,
        "query_kind": None,
        "start_date": None,
        "end_date": None,
        "compare_start_date": None,
        "compare_end_date": None,
        "limit": None,
        "reference": {
            "last": True,
            "transaction_type": None,
            "category_id": None,
            "subcategory_id": None,
            "description": None,
            "amount": None,
            "transaction_date": None,
            "start_date": None,  # v1.5.2
            "end_date": None,  # v1.5.2
            "all": False,  # v1.5.2
            "transaction_id": "abc",
        },
        "updates": None,
        "scope": None,  # v1.2
        "target_member_name": None,  # v1.2
        "for_another_person": False,  # v1.5.1
        "correction": False,  # v1.5.2
        "months": None,  # v1.5
        "trend_metric": None,  # v1.5
        "trend_direction": None,  # v1.5
    }
    from apps.ai.contract import InvalidOutput

    with pytest.raises(InvalidOutput):
        parse_output(data)
    del data["reference"]["transaction_id"]
    parsed = parse_output(data)
    assert parsed.reference.last is True


# --- Follow-up context --------------------------------------------------------------------


def month(d):
    return d.replace(day=1)


def prev_month(d):
    end = month(d) - timedelta(days=1)
    return end.replace(day=1), end


def query(**fields):
    return interp(intent="QUERY_TRANSACTIONS", **fields)


@pytest.fixture
def spending(alice, maria, catalog, make_transaction):
    d = today()
    p_start, _ = prev_month(d)
    t = make_transaction
    t(user=alice, category=catalog["transporte"], amount="54.60", transaction_date=d)
    t(user=alice, category=catalog["alimentacao"], amount="32.40", transaction_date=d)
    t(user=alice, category=catalog["transporte"], amount="40.00", transaction_date=p_start)
    t(user=maria, category=catalog["transporte"], amount="999.00", transaction_date=d)


def test_period_follow_up(client, alice, catalog, spending, ai, evolution):
    d = today()
    ai.set(
        query(
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            category_id=catalog["transporte"].pk,
            start_date=month(d).isoformat(),
            end_date=d.isoformat(),
        )
    )
    deliver(client, "quanto gastei de transporte esse mês?", "Q1")
    assert "R$ 54,60" in last_reply(evolution)

    p_start, p_end = prev_month(d)
    ai.set(
        query(
            follow_up=True,
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            category_id=catalog["transporte"].pk,
            start_date=p_start.isoformat(),
            end_date=p_end.isoformat(),
        )
    )
    deliver(client, "e mês passado?", "Q2")
    assert "R$ 40,00 com Transporte" in last_reply(evolution)

    # The previous (validated) query was sent to the model as structured context.
    context = ai.call_args.args[1]
    assert context.previous_query["query_kind"] == "TOTAL"
    assert context.previous_query["category_id"] == catalog["transporte"].pk
    assert set(context.previous_query) == {
        "query_kind",
        "transaction_type",
        "category_id",
        "subcategory_id",
        "start_date",
        "end_date",
        "compare_start_date",
        "compare_end_date",
        "limit",
        # v1.2: whose data (semantics only; re-authorized on every query)
        "scope",
        "target_member_name",
        # v1.5: analytics semantics (validated values)
        "months",
        "trend_metric",
        "trend_direction",
    }
    assert context.previous_query["scope"] == "SELF"


def test_category_follow_up(client, alice, catalog, spending, ai, evolution):
    d = today()
    ai.set(
        query(query_kind="TOTAL", transaction_type="EXPENSE", category_id=catalog["alimentacao"].pk)
    )
    deliver(client, "quanto gastei com alimentação?", "Q1")
    ai.set(
        query(
            follow_up=True,
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            category_id=catalog["transporte"].pk,
        )
    )
    deliver(client, "e transporte?", "Q2")
    assert last_reply(evolution) == (
        f"Você gastou R$ 54,60 com Transporte {period_text(month(d), d)}."
    )


def test_follow_up_without_context(client, alice, catalog, ai, evolution):
    ai.set(query(follow_up=True, query_kind="TOTAL", transaction_type="EXPENSE"))
    record = deliver(client, "e mês passado?")
    record.refresh_from_db()
    assert record.error_code == "NO_CONTEXT"
    assert last_reply(evolution) == messages.NO_CONTEXT
    assert ai.call_args.args[1].previous_query is None


@override_settings(CONVERSATION_CONTEXT_TTL_MINUTES=20)
def test_context_expires(client, alice, catalog, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE"))
    deliver(client, "quanto gastei?", "Q1")
    ConversationContext.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    ai.set(query(follow_up=True, query_kind="TOTAL", transaction_type="EXPENSE"))
    record = deliver(client, "e mês passado?", "Q2")
    record.refresh_from_db()
    assert record.error_code == "NO_CONTEXT"
    assert ai.call_args.args[1].previous_query is None


def test_context_is_per_user(client, alice, maria, catalog, ai, evolution):
    ConversationContext.objects.create(
        user=maria,
        last_query={"query_kind": "TOTAL", "category_id": catalog["transporte"].pk},
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    ai.set(query(follow_up=True, query_kind="TOTAL", transaction_type="EXPENSE"))
    record = deliver(client, "e mês passado?")  # sent by Alice
    record.refresh_from_db()
    assert record.error_code == "NO_CONTEXT"
    assert ai.call_args.args[1].previous_query is None


def test_context_does_not_grant_access(client, alice, maria, catalog, spending, ai, evolution):
    """Even with context, a follow-up only ever reads the sender's own data."""
    ai.set(
        query(query_kind="TOTAL", transaction_type="EXPENSE", category_id=catalog["transporte"].pk)
    )
    deliver(client, "quanto gastei de transporte?", "Q1")
    ai.set(
        query(
            follow_up=True,
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            category_id=catalog["transporte"].pk,
        )
    )
    deliver(client, "e a Maria?", "Q2")
    assert "999" not in last_reply(evolution)
    assert "R$ 54,60" in last_reply(evolution)


def test_mutations_do_not_overwrite_query_context(client, alice, catalog, ai, evolution):
    ai.set(query(query_kind="BALANCE"))
    deliver(client, "saldo", "Q1")
    ai.set(gas_proposal(catalog))
    deliver(client, "10 de gasolina", "M1")
    assert ConversationContext.objects.get(user=alice).last_query["query_kind"] == "BALANCE"


# --- Advanced queries -------------------------------------------------------------------------


@pytest.fixture
def october(alice, maria, catalog, make_transaction):
    d = today()
    t = make_transaction
    rows = [
        ("mercado", "alimentacao", "180.00"),
        ("gasolina", "transporte", "120.00"),
        ("restaurante", "alimentacao", "80.00"),
        ("uber", "transporte", "15.50"),
        ("padaria", "alimentacao", "9.90"),
        ("pedágio", "transporte", "7.00"),
    ]
    for description, category, amount in rows:
        t(
            user=alice,
            category=catalog[category],
            amount=amount,
            description=description,
            transaction_date=d,
        )
    deleted = t(
        user=alice,
        category=catalog["transporte"],
        amount="5000.00",
        description="apagado",
        transaction_date=d,
    )
    deleted.deleted_at = timezone.now()
    deleted.save()
    t(
        user=maria,
        category=catalog["transporte"],
        amount="7777.00",
        description="maria",
        transaction_date=d,
    )
    t(user=alice, category=catalog["salario"], type="INCOME", amount="3000.00", transaction_date=d)


def period_text(start, end):
    from common.formatting import describe_period

    return describe_period(start, end, today())


def test_top_expenses_default_limit(client, alice, october, ai, evolution):
    ai.set(query(query_kind="TOP"))
    deliver(client, "quais foram meus maiores gastos esse mês?")
    d = today()
    reply = last_reply(evolution)
    assert reply.startswith(f"Seus maiores gastos {period_text(month(d), d)} foram:")
    assert rows(reply) == [
        "Mercado: R$ 180,00",
        "Gasolina: R$ 120,00",
        "Restaurante: R$ 80,00",
        "Uber: R$ 15,50",
        "Padaria: R$ 9,90",
    ]


@pytest.mark.parametrize("limit, lines", [(2, 2), (50, 6), (0, 5)])
def test_top_expenses_limits(client, alice, october, ai, evolution, limit, lines):
    ai.set(query(query_kind="TOP", limit=limit))
    deliver(client, "me mostra os maiores gastos")
    reply = last_reply(evolution)
    assert len(rows(reply)) == lines  # max 10; only 6 active expenses exist
    assert "5.000" not in reply and "7.777" not in reply  # deleted / other user


def test_breakdown_by_category(client, alice, october, ai, evolution):
    ai.set(query(query_kind="BREAKDOWN"))
    deliver(client, "onde eu mais gastei esse mês?")
    d = today()
    reply = last_reply(evolution)
    assert reply.startswith(f"{period_text(month(d), d).capitalize()}, seus gastos por categoria")
    assert rows(reply) == ["Alimentação: R$ 269,90", "Transporte: R$ 142,50"]


def test_breakdown_empty(client, alice, catalog, ai, evolution):
    ai.set(query(query_kind="BREAKDOWN"))
    deliver(client, "quanto gastei por categoria?")
    assert last_reply(evolution).startswith("Não encontrei gastos")


def test_list_recent(client, alice, october, ai, evolution):
    ai.set(query(query_kind="LIST", transaction_type="EXPENSE", limit=3))
    deliver(client, "quais foram meus últimos gastos?")
    reply = last_reply(evolution)
    assert reply.startswith("Estes são seus últimos gastos:")
    assert len(rows(reply)) == 3
    assert rows(reply)[0].startswith("Pedágio")  # newest registered first
    assert "Maria" not in reply and "Apagado" not in reply


def test_list_by_period_and_category(
    client, alice, october, catalog, ai, evolution, make_transaction
):
    yesterday = today() - timedelta(days=1)
    make_transaction(
        user=alice,
        category=catalog["transporte"],
        amount="3.00",
        description="ônibus",
        transaction_date=yesterday,
    )
    ai.set(
        query(
            query_kind="LIST",
            transaction_type="EXPENSE",
            category_id=catalog["transporte"].pk,
            start_date=yesterday.isoformat(),
            end_date=yesterday.isoformat(),
        )
    )
    deliver(client, "me mostra os gastos de transporte de ontem")
    reply = last_reply(evolution)
    assert reply.startswith("Estes foram seus gastos ontem:")
    assert rows(reply) == [f"Ônibus: R$ 3,00 ({yesterday:%d/%m})"]


def test_compare_periods(client, alice, catalog, ai, evolution, make_transaction):
    d = today()
    p_start, p_end = prev_month(d)
    make_transaction(
        user=alice, category=catalog["transporte"], amount="850.00", transaction_date=d
    )
    make_transaction(
        user=alice, category=catalog["transporte"], amount="700.00", transaction_date=p_start
    )
    ai.set(
        query(
            query_kind="COMPARE",
            transaction_type="EXPENSE",
            start_date=month(d).isoformat(),
            end_date=d.isoformat(),
            compare_start_date=p_start.isoformat(),
            compare_end_date=p_end.isoformat(),
        )
    )
    deliver(client, "compare esse mês com o mês passado")
    assert last_reply(evolution) == (
        f"Você gastou R$ 850,00 {period_text(month(d), d)} e R$ 700,00 "
        f"{period_text(p_start, p_end)}. Foram R$ 150,00 a mais (+21,4%)."
    )


def test_compare_defaults_to_previous_month(
    client, alice, catalog, ai, evolution, make_transaction
):
    d = today()
    p_start, p_end = prev_month(d)
    make_transaction(
        user=alice, category=catalog["transporte"], amount="100.00", transaction_date=d
    )
    ai.set(query(query_kind="COMPARE"))
    deliver(client, "gastei mais esse mês ou mês passado?")
    reply = last_reply(evolution)
    assert "R$ 100,00 a mais" in reply
    assert reply.endswith("não houve gastos no período anterior.")


def test_compare_equal_and_income(client, alice, catalog, ai, evolution, make_transaction):
    d = today()
    p_start, p_end = prev_month(d)
    for day in (d, p_start):
        make_transaction(
            user=alice,
            category=catalog["salario"],
            type="INCOME",
            amount="3000",
            transaction_date=day,
        )
    ai.set(query(query_kind="COMPARE", transaction_type="INCOME"))
    deliver(client, "recebi mais esse mês?")
    reply = last_reply(evolution)
    assert reply.startswith("Você recebeu R$ 3.000,00")
    assert reply.endswith("Foi o mesmo valor nos dois períodos.")


def test_existing_total_and_balance_still_work(client, alice, october, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE"))
    deliver(client, "quanto gastei esse mês?", "Q1")
    assert "R$ 412,40" in last_reply(evolution)
    ai.set(query(query_kind="BALANCE"))
    deliver(client, "qual meu saldo esse mês?", "Q2")
    assert "R$ 2.587,60" in last_reply(evolution)


def test_one_ai_call_per_message(client, alice, catalog, ai, evolution):
    for index, proposal in enumerate(
        [gas_proposal(catalog), update(LAST, amount="15"), UNDO, query(query_kind="TOP")]
    ):
        ai.set(proposal)
        deliver(client, "x", f"M{index}")
    assert ai.call_count == 4


def test_unknown_period_dates_rejected(client, alice, catalog, ai, evolution):
    ai.set(query(query_kind="COMPARE", compare_start_date="2026-13-01"))
    record = deliver(client, "compare")
    record.refresh_from_db()
    assert record.error_code == "INVALID_DATE"


def test_catalog_reference_keeps_the_description_criterion(
    client, alice, catalog, ai, evolution, make_transaction
):
    """Audit P1#2: category/subcategory never replace the description — both are applied (AND).
    The only fuel expense is described "posto shell", not "gasolina" → not found, unchanged."""
    txn = make_transaction(
        user=alice,
        category=catalog["transporte"],
        subcategory=catalog["combustivel"],
        amount="30",
        description="posto shell",
        transaction_date=today(),
    )
    ai.set(
        update(
            TransactionReference(subcategory_id=catalog["combustivel"].pk, description="gasolina"),
            amount="20",
        )
    )
    record = deliver(client, "aquele gasto de gasolina era 20 reais")
    txn.refresh_from_db()
    assert txn.amount == Decimal("30.00")
    record.refresh_from_db()
    assert record.error_code == "NOT_FOUND"
