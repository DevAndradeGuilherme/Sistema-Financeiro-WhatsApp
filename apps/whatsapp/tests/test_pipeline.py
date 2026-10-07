"""Textual pipeline: webhook → (mocked) AI → Django validation → domain → (mocked) Evolution.

The AI mock returns what the model is expected to propose for each pt-BR message; these tests
check that Django validates, authorizes, computes and replies on its own.
"""

import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest

from apps.ai.contract import Interpretation
from apps.ai.service import AIError, InterpretationResult
from apps.categories.models import Category, Subcategory
from apps.categories.seed import seed_categories
from apps.transactions.models import Transaction, TransactionSource
from apps.whatsapp.client import EvolutionHTTPError
from apps.whatsapp.models import InboundMessage
from apps.whatsapp.pipeline import handle_ready_message
from common.dates import today_local
from common.formatting import format_date

from . import payloads

pytestmark = pytest.mark.django_db

URL = "/api/v1/webhooks/evolution/"
SECRET = "test-webhook-secret"
Status = InboundMessage.Status


def interp(**fields) -> Interpretation:
    base = dict.fromkeys(
        [
            "transaction_type",
            "amount",
            "description",
            "category_id",
            "subcategory_id",
            "transaction_date",
            "query_kind",
            "start_date",
            "end_date",
        ]
    )
    base["intent"] = "UNKNOWN"
    base.update(fields)
    return Interpretation(**base)


@pytest.fixture
def catalog(db):
    seed_categories()
    return {
        "transporte": Category.objects.get(slug="transporte"),
        "combustivel": Subcategory.objects.get(slug="combustivel"),
        "uber": Subcategory.objects.get(slug="uber"),
        "alimentacao": Category.objects.get(slug="alimentacao"),
        "salario": Category.objects.get(slug="salario"),
        "outros_gastos": Category.objects.get(slug="outros-gastos"),
        "outras_receitas": Category.objects.get(slug="outras-receitas"),
    }


@pytest.fixture
def alice(make_user):
    return make_user(username="alice", phone_number="+5531999990001")


@pytest.fixture
def maria(make_user):
    return make_user(username="maria", phone_number="+5521988887777", first_name="Maria")


@pytest.fixture
def ai():
    with mock.patch("apps.whatsapp.pipeline.interpret_message") as interpret:
        interpret.set = lambda interpretation: setattr(
            interpret,
            "return_value",
            InterpretationResult(interpretation, model="test-model", duration_ms=5),
        )
        yield interpret


@pytest.fixture
def evolution():
    with mock.patch("apps.whatsapp.pipeline.EvolutionClient") as cls:
        yield cls.return_value.send_text


def deliver(client, text, message_id="MSG1", **kwargs):
    payload = payloads.text(text, message_id=message_id, **kwargs)
    response = client.post(
        URL,
        data=json.dumps(payload),
        content_type="application/json",
        headers={"X-Webhook-Secret": SECRET},
    )
    assert response.status_code == 200
    return InboundMessage.objects.get(provider_message_id=message_id)


def reply_of(evolution) -> str:
    evolution.assert_called_once()
    phone, text = evolution.call_args.args
    assert phone == "+5531999990001"
    return text


def today():
    return today_local()


def iso(d):
    return d.isoformat()


# --- CREATE --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message, amount, expected",
    [
        ("10 reais de gasolina", "10.00", Decimal("10.00")),
        ("R$ 10,50 de gasolina", "10.50", Decimal("10.50")),
        ("gastei dez reais de gasolina", "10.00", Decimal("10.00")),
    ],
)
def test_create_expense(client, alice, catalog, ai, evolution, message, amount, expected):
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="EXPENSE",
            amount=amount,
            description="gasolina",
            category_id=catalog["transporte"].pk,
            subcategory_id=catalog["combustivel"].pk,
        )
    )
    record = deliver(client, message)

    txn = Transaction.objects.get()
    assert txn.user == alice
    assert txn.type == "EXPENSE"
    assert txn.amount == expected
    assert txn.category == catalog["transporte"]
    assert txn.subcategory == catalog["combustivel"]
    assert txn.transaction_date == today()
    assert txn.source == TransactionSource.TEXT
    assert txn.idempotency_key == "whatsapp:test-instance:MSG1"

    record.refresh_from_db()
    assert record.status == Status.PROCESSED
    assert record.intent == "CREATE_TRANSACTION"
    assert record.transaction == txn
    assert record.error_code == ""
    ai.assert_called_once()
    amount_text = "10,50" if expected == Decimal("10.50") else "10,00"
    assert (
        reply_of(evolution) == f"✅ Gasto registrado: R$ {amount_text} em Transporte · Combustível."
    )


def test_expense_with_relative_date(client, alice, catalog, ai, evolution):
    yesterday = today() - timedelta(days=1)
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="EXPENSE",
            amount="40",
            category_id=catalog["transporte"].pk,
            subcategory_id=catalog["combustivel"].pk,
            transaction_date=iso(yesterday),
        )
    )
    deliver(client, "gastei 40 de gasolina ontem")
    txn = Transaction.objects.get()
    assert txn.transaction_date == yesterday
    assert txn.amount == Decimal("40.00")
    assert reply_of(evolution).endswith("Transporte · Combustível (ontem).")


def test_expense_in_other_category(client, alice, catalog, ai, evolution):
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="EXPENSE",
            amount="35.90",
            description="mercado",
            category_id=catalog["alimentacao"].pk,
        )
    )
    deliver(client, "gastei 35,90 no mercado")
    txn = Transaction.objects.get()
    assert (txn.amount, txn.category, txn.description) == (
        Decimal("35.90"),
        catalog["alimentacao"],
        "mercado",
    )
    assert reply_of(evolution) == "✅ Gasto registrado: R$ 35,90 em Alimentação."


def test_create_income(client, alice, catalog, ai, evolution):
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="INCOME",
            amount="2185",
            category_id=catalog["salario"].pk,
        )
    )
    deliver(client, "recebi 2185 de salário")
    txn = Transaction.objects.get()
    assert (txn.type, txn.amount, txn.category) == (
        "INCOME",
        Decimal("2185.00"),
        catalog["salario"],
    )
    assert reply_of(evolution) == "✅ Entrada registrada: R$ 2.185,00 em Salário."


def test_income_without_category_uses_income_fallback(client, alice, catalog, ai, evolution):
    yesterday = today() - timedelta(days=1)
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="INCOME",
            amount="100",
            transaction_date=iso(yesterday),
        )
    )
    deliver(client, "recebi 100 ontem")
    txn = Transaction.objects.get()
    assert txn.category == catalog["outras_receitas"]
    assert txn.transaction_date == yesterday


def test_expense_without_category_uses_expense_fallback(client, alice, catalog, ai, evolution):
    ai.set(interp(intent="CREATE_TRANSACTION", transaction_type="EXPENSE", amount="12"))
    deliver(client, "gastei 12 com uma coisa")
    assert Transaction.objects.get().category == catalog["outros_gastos"]


def test_fallback_follows_flag_not_name(client, alice, catalog, ai, evolution):
    catalog["outros_gastos"].name = "Diversos"
    catalog["outros_gastos"].save()
    ai.set(interp(intent="CREATE_TRANSACTION", transaction_type="EXPENSE", amount="12"))
    deliver(client, "gastei 12")
    assert Transaction.objects.get().category.name == "Diversos"


def test_unknown_category_id_falls_back(client, alice, catalog, ai, evolution):
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="EXPENSE",
            amount="12",
            category_id=999999,
            subcategory_id=888888,
        )
    )
    deliver(client, "gastei 12")
    txn = Transaction.objects.get()
    assert txn.category == catalog["outros_gastos"]
    assert txn.subcategory is None


def test_category_of_wrong_type_falls_back(client, alice, catalog, ai, evolution):
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="EXPENSE",
            amount="12",
            category_id=catalog["salario"].pk,  # INCOME category for an expense
        )
    )
    deliver(client, "gastei 12")
    assert Transaction.objects.get().category == catalog["outros_gastos"]


def test_incompatible_subcategory_is_dropped(client, alice, catalog, ai, evolution):
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="EXPENSE",
            amount="20",
            category_id=catalog["alimentacao"].pk,
            subcategory_id=catalog["combustivel"].pk,  # belongs to Transporte
        )
    )
    deliver(client, "gastei 20")
    txn = Transaction.objects.get()
    assert txn.category == catalog["alimentacao"]
    assert txn.subcategory is None
    assert reply_of(evolution) == "✅ Gasto registrado: R$ 20,00 em Alimentação."


def test_subcategory_alone_implies_its_category(client, alice, catalog, ai, evolution):
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="EXPENSE",
            amount="30",
            subcategory_id=catalog["uber"].pk,
        )
    )
    deliver(client, "sexta gastei 30 de uber")
    txn = Transaction.objects.get()
    assert (txn.category, txn.subcategory) == (catalog["transporte"], catalog["uber"])


# --- CREATE errors -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields, code, reply",
    [
        (
            {"amount": None},
            "MISSING_AMOUNT",
            "Não identifiquei o valor. Envie de novo com o valor, por exemplo: "
            "25 reais de gasolina.",
        ),
        (
            {"amount": "abc"},
            "INVALID_AMOUNT",
            "Não entendi o valor. Envie, por exemplo: 25,90 de mercado.",
        ),
        ({"amount": "0"}, "REJECTED_AMOUNT", "Não registrei: O valor deve ser maior que zero."),
        ({"amount": "-10"}, "REJECTED_AMOUNT", "Não registrei: O valor deve ser maior que zero."),
        (
            {"amount": "10.555"},
            "REJECTED_AMOUNT",
            "Não registrei: O valor deve ter no máximo 2 casas decimais.",
        ),
        (
            {"amount": "5000000"},
            "REJECTED_AMOUNT",
            "Não registrei: O valor excede o limite permitido por transação.",
        ),
        (
            {"transaction_date": "2026-02-30"},
            "INVALID_DATE",
            "Não entendi a data. Tente, por exemplo: 'ontem' ou 'dia 5'.",
        ),
        ({"transaction_type": None}, "NOT_UNDERSTOOD", None),
    ],
)
def test_invalid_create_is_answered_without_transaction(
    client, alice, catalog, ai, evolution, fields, code, reply
):
    base = {
        "intent": "CREATE_TRANSACTION",
        "transaction_type": "EXPENSE",
        "amount": "10",
        "category_id": catalog["transporte"].pk,
    }
    ai.set(interp(**{**base, **fields}))
    record = deliver(client, "gastei gasolina")
    assert not Transaction.objects.exists()
    record.refresh_from_db()
    assert record.status == Status.PROCESSED
    assert record.error_code == code
    if reply:
        assert reply_of(evolution) == reply


def test_future_date_rejected(client, alice, catalog, ai, evolution):
    tomorrow = today() + timedelta(days=1)
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="EXPENSE",
            amount="10",
            transaction_date=iso(tomorrow),
        )
    )
    record = deliver(client, "gastei 10 amanhã")
    assert not Transaction.objects.exists()
    record.refresh_from_db()
    assert record.error_code == "REJECTED_TRANSACTION_DATE"
    assert reply_of(evolution) == "Não registrei: Data da transação não pode estar no futuro."


# --- QUERY ---------------------------------------------------------------------------------


@pytest.fixture
def ledger(alice, maria, catalog, make_transaction):
    t = make_transaction
    d = today()
    t(
        user=alice,
        category=catalog["transporte"],
        subcategory=catalog["combustivel"],
        amount="50.10",
        transaction_date=d,
    )
    t(user=alice, category=catalog["transporte"], amount="4.50", transaction_date=d)
    t(user=alice, category=catalog["alimentacao"], amount="32.40", transaction_date=d)
    t(user=alice, category=catalog["salario"], type="INCOME", amount="3000.00", transaction_date=d)
    # Maria's data must never appear in Alice's answers.
    t(user=maria, category=catalog["transporte"], amount="999.00", transaction_date=d)
    t(user=maria, category=catalog["salario"], type="INCOME", amount="8000.00", transaction_date=d)


def month(d):
    return iso(d.replace(day=1)), iso(d)


def period_text(start, end):
    from common.formatting import describe_period

    return describe_period(start, end, today())


@pytest.mark.parametrize(
    "message, fields, total, scope",
    [
        ("quanto gastei esse mês?", {"transaction_type": "EXPENSE"}, "R$ 87,00", ""),
        (
            "quanto gastei de transporte esse mês?",
            {"transaction_type": "EXPENSE", "category": "transporte"},
            "R$ 54,60",
            " com Transporte",
        ),
        (
            "quanto gastei com gasolina esse mês?",
            {"transaction_type": "EXPENSE", "subcategory": "combustivel"},
            "R$ 50,10",
            " com Combustível",
        ),
        ("quanto recebi esse mês?", {"transaction_type": "INCOME"}, "R$ 3.000,00", ""),
    ],
)
def test_total_queries(
    client, alice, catalog, ledger, ai, evolution, message, fields, total, scope
):
    d = today()
    start, end = month(d)
    proposal = {
        "intent": "QUERY_TRANSACTIONS",
        "query_kind": "TOTAL",
        "transaction_type": fields["transaction_type"],
        "start_date": start,
        "end_date": end,
    }
    if "category" in fields:
        proposal["category_id"] = catalog[fields["category"]].pk
    if "subcategory" in fields:
        proposal["subcategory_id"] = catalog[fields["subcategory"]].pk
    ai.set(interp(**proposal))
    count_before = Transaction.objects.count()

    record = deliver(client, message)

    verb = "recebeu" if fields["transaction_type"] == "INCOME" else "gastou"
    expected = f"Você {verb} {total}{scope} {period_text(d.replace(day=1), d)}."
    assert reply_of(evolution) == expected
    assert Transaction.objects.count() == count_before
    record.refresh_from_db()
    assert (record.status, record.intent, record.transaction) == (
        Status.PROCESSED,
        "QUERY_TRANSACTIONS",
        None,
    )


def test_balance_query(client, alice, ledger, ai, evolution):
    d = today()
    start, end = month(d)
    ai.set(
        interp(intent="QUERY_TRANSACTIONS", query_kind="BALANCE", start_date=start, end_date=end)
    )
    deliver(client, "qual meu saldo esse mês?")
    assert reply_of(evolution) == (
        f"Seu saldo {period_text(d.replace(day=1), d)} é R$ 2.913,00 "
        "(entradas R$ 3.000,00; gastos R$ 87,00)."
    )


def test_query_today_and_yesterday(client, alice, catalog, ledger, ai, evolution, make_transaction):
    d = today()
    yesterday = d - timedelta(days=1)
    make_transaction(
        user=alice, category=catalog["alimentacao"], amount="7.00", transaction_date=yesterday
    )

    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            start_date=iso(d),
            end_date=iso(d),
        )
    )
    deliver(client, "quanto gastei hoje?", message_id="Q1")
    assert evolution.call_args.args[1] == "Você gastou R$ 87,00 hoje."

    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            start_date=iso(yesterday),
            end_date=iso(yesterday),
        )
    )
    deliver(client, "quanto gastei ontem?", message_id="Q2")
    assert evolution.call_args.args[1] == "Você gastou R$ 7,00 ontem."


def test_query_without_period_defaults_to_current_month(client, alice, ledger, ai, evolution):
    ai.set(interp(intent="QUERY_TRANSACTIONS", query_kind="TOTAL", transaction_type="EXPENSE"))
    deliver(client, "quanto gastei?")
    d = today()
    assert reply_of(evolution) == f"Você gastou R$ 87,00 {period_text(d.replace(day=1), d)}."


def test_invalid_period_rejected(client, alice, ledger, ai, evolution):
    d = today()
    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            start_date=iso(d),
            end_date=iso(d - timedelta(days=3)),
        )
    )
    record = deliver(client, "quanto gastei?")
    record.refresh_from_db()
    assert record.error_code == "INVALID_PERIOD"
    assert reply_of(evolution).startswith("Período inválido")


@pytest.mark.parametrize("category_key", ["salario", None])
def test_query_with_invalid_category_is_rejected(
    client, alice, catalog, ai, evolution, category_key
):
    category_id = catalog[category_key].pk if category_key else 999999
    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            category_id=category_id,
        )
    )
    record = deliver(client, "quanto gastei de x?")
    record.refresh_from_db()
    assert record.error_code == "CATEGORY_NOT_FOUND"


# --- Security ------------------------------------------------------------------------------


def test_query_about_other_user_only_returns_own_data(client, alice, maria, ledger, ai, evolution):
    """Even if the model treats "quanto a Maria gastou?" as a query, the user is the sender."""
    ai.set(interp(intent="QUERY_TRANSACTIONS", query_kind="TOTAL", transaction_type="EXPENSE"))
    deliver(client, "quanto a Maria gastou esse mês?")
    reply = reply_of(evolution)
    assert "R$ 87,00" in reply  # Alice's own expenses
    assert "999" not in reply and "1.086" not in reply


def test_payload_cannot_change_owner(client, alice, maria, catalog, ai, evolution):
    ai.set(interp(intent="CREATE_TRANSACTION", transaction_type="EXPENSE", amount="10"))
    payload = payloads.text(
        "10 reais", message_id="X1", key_extra={"participant": "5521988887777@s.whatsapp.net"}
    )
    payload["data"]["userId"] = maria.pk
    payload["data"]["owner"] = "maria"
    client.post(
        URL,
        data=json.dumps(payload),
        content_type="application/json",
        headers={"X-Webhook-Secret": SECRET},
    )
    assert Transaction.objects.get().user == alice


def test_ai_receives_only_text_and_catalog(client, alice, catalog, ai, evolution):
    from apps.ai.prompts import build_instructions

    ai.set(interp(intent="HELP"))
    deliver(client, "ajuda")
    text, context = ai.call_args.args
    assert text == "ajuda"
    instructions = build_instructions(context)
    for private in ("+5531999990001", "5531999990001", "alice", str(alice.pk)):
        assert private not in repr(context)
    assert "+5531999990001" not in instructions
    assert "alice" not in instructions
    assert context.timezone == "America/Sao_Paulo"
    assert context.reference_date == today()
    # Only active catalog entries are sent.
    catalog["alimentacao"].is_active = False
    catalog["alimentacao"].save()
    deliver(client, "ajuda", message_id="H2")
    assert "Alimentação" not in build_instructions(ai.call_args.args[1])


def test_help_and_unknown_are_local(client, alice, catalog, ai, evolution):
    ai.set(interp(intent="HELP"))
    deliver(client, "o que você faz?", message_id="H1")
    assert evolution.call_args.args[1].startswith("Você pode registrar gastos e receitas")

    ai.set(interp(intent="UNKNOWN"))
    record = deliver(client, "bom dia", message_id="U1")
    assert evolution.call_args.args[1].startswith("Não consegui entender.")
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "NOT_UNDERSTOOD")
    assert ai.call_count == 2  # exactly one AI call per message
    assert not Transaction.objects.exists()


# --- Idempotency and consistency ---------------------------------------------------------------


def create_proposal(catalog):
    return interp(
        intent="CREATE_TRANSACTION",
        transaction_type="EXPENSE",
        amount="10",
        category_id=catalog["transporte"].pk,
    )


def test_duplicate_delivery_runs_business_once(client, alice, catalog, ai, evolution):
    ai.set(create_proposal(catalog))
    deliver(client, "10 reais de gasolina")
    deliver(client, "10 reais de gasolina")
    assert ai.call_count == 1
    assert Transaction.objects.count() == 1
    assert evolution.call_count == 1


def test_reply_failure_does_not_recreate_transaction(client, alice, catalog, ai, evolution):
    ai.set(create_proposal(catalog))
    evolution.side_effect = EvolutionHTTPError(500, "down")
    record = deliver(client, "10 reais de gasolina")

    record.refresh_from_db()
    assert record.status == Status.PROCESSED
    assert record.error_code == "REPLY_SEND_FAILED"
    assert Transaction.objects.count() == 1

    # Redelivery and a direct re-run are both no-ops.
    deliver(client, "10 reais de gasolina")
    handle_ready_message(record)
    assert Transaction.objects.count() == 1
    assert ai.call_count == 1


@pytest.mark.parametrize(
    "code",
    [
        "TIMEOUT",
        "AUTH",
        "RATE_LIMIT",
        "PROVIDER_ERROR",
        "INCOMPLETE",
        "INVALID_OUTPUT",
        "REFUSAL",
        "MODEL_UNAVAILABLE",
    ],
)
def test_ai_failure_creates_nothing(client, alice, catalog, ai, evolution, code):
    ai.side_effect = AIError(code)
    record = deliver(client, "10 reais de gasolina")
    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert record.error_code == f"AI_{code}"
    assert not Transaction.objects.exists()
    assert reply_of(evolution) == (
        "Não consegui processar sua mensagem agora. Tente novamente em alguns instantes."
    )


def test_failure_while_marking_processed_rolls_back_transaction(
    client, alice, catalog, ai, evolution
):
    ai.set(create_proposal(catalog))
    original_save = InboundMessage.save

    def failing_save(self, *args, **kwargs):
        if self.status == Status.PROCESSED:
            raise RuntimeError("crash after effect, before commit")
        return original_save(self, *args, **kwargs)

    with mock.patch.object(InboundMessage, "save", failing_save):
        record = deliver(client, "10 reais de gasolina")
    record.refresh_from_db()
    assert not Transaction.objects.exists()  # effect rolled back with the status change
    assert record.status == Status.FAILED
    assert record.error_code == "PIPELINE_ERROR"


def test_missing_fallback_fails_without_effect(client, alice, catalog, ai, evolution):
    Category.objects.update(is_fallback=False)
    ai.set(interp(intent="CREATE_TRANSACTION", transaction_type="EXPENSE", amount="10"))
    record = deliver(client, "gastei 10")
    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert not Transaction.objects.exists()


def test_message_processed_concurrently_is_not_executed_again(
    client, alice, catalog, ai, evolution
):
    ai.set(create_proposal(catalog))

    def processed_elsewhere(*args, **kwargs):
        InboundMessage.objects.update(status=Status.PROCESSED)
        return ai.return_value

    ai.side_effect = processed_elsewhere
    deliver(client, "10 reais de gasolina")
    assert not Transaction.objects.exists()
    evolution.assert_not_called()


def test_non_ready_messages_are_never_sent_to_ai(alice, ai):
    for status in (Status.RECEIVED, Status.IGNORED, Status.PROCESSED, Status.FAILED):
        record = InboundMessage.objects.create(
            instance="test-instance",
            provider_message_id=f"N-{status}",
            message_type="TEXT",
            status=status,
            user=alice,
            text="10 reais",
        )
        handle_ready_message(record)
    ai.assert_not_called()


def test_reply_shows_saved_values(client, alice, catalog, ai, evolution):
    yesterday = today() - timedelta(days=2)
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="EXPENSE",
            amount="1234.5",
            category_id=catalog["transporte"].pk,
            transaction_date=iso(yesterday),
        )
    )
    deliver(client, "1234,50 de transporte")
    txn = Transaction.objects.get()
    assert reply_of(evolution) == (
        f"✅ Gasto registrado: R$ 1.234,50 em {txn.category.name} "
        f"(em {format_date(txn.transaction_date)})."
    )
