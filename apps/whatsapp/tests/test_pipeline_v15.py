"""V1.5 analytics through the pipeline: PERIOD_SUMMARY, MONTHLY_TREND, CATEGORY_TREND.

The AI mock returns only the semantic proposal; Django authorizes (V1.2 scopes), PostgreSQL
aggregates, and the reply is formatted locally. Domain math is covered in
``apps/transactions/tests/test_analytics.py``; here we check wiring, authorization, context
and side effects.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.families.models import FamilyMember
from apps.families.services import add_family_member, create_family
from apps.transactions.models import Transaction, TransactionChange
from apps.transactions.services import soft_delete_transaction
from apps.whatsapp.client import EvolutionHTTPError
from apps.whatsapp.models import ConversationContext, InboundMessage
from apps.whatsapp.pipeline import handle_ready_message
from common.dates import today_local
from common.formatting import describe_period

from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db
Status = InboundMessage.Status

MARIA_JID = "5521988887777@s.whatsapp.net"  # maria (fictitious)
JOAO_JID = "5531977770003@s.whatsapp.net"  # joão (fictitious)
MONTH_ABBR = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


def today():
    return today_local()


def month_start(offset=0):
    """First day of the month ``offset`` months before the current one."""
    first = today().replace(day=1)
    for _ in range(offset):
        first = (first - timedelta(days=1)).replace(day=1)
    return first


def month():
    return month_start(), today()


def prev_month():
    end = month_start() - timedelta(days=1)
    return end.replace(day=1), end


def period_text(start, end):
    return describe_period(start, end, today())


def reply(evolution):
    return evolution.call_args.args[1]


def query(**fields):
    return interp(intent="QUERY_TRANSACTIONS", **fields)


def trend_lines(values_by_offset: dict[int, str], months: int) -> list[str]:
    """Expected '<mês> — R$ x' lines, oldest first (year suffix when the window spans years)."""
    window = [month_start(offset) for offset in reversed(range(months))]
    years = {m.year for m in window}
    lines = []
    for offset, m in zip(reversed(range(months)), window, strict=True):
        label = MONTH_ABBR[m.month - 1] + (f"/{m.year}" if len(years) > 1 else "")
        lines.append(f"{label} — {values_by_offset.get(offset, 'R$ 0,00')}")
    return lines


def snapshot():
    return (
        {(t.pk, t.amount, t.deleted_at) for t in Transaction.objects.all()},
        TransactionChange.objects.count(),
    )


@pytest.fixture
def family(alice, maria, make_user):
    alice.first_name = "Alice"
    alice.save()
    joao = make_user(username="joao", first_name="João", phone_number="+5531977770003")
    fam = create_family(name="Família DEV", admin=alice)
    add_family_member(family=fam, user=maria)
    add_family_member(family=fam, user=joao)
    return {"family": fam, "joao": joao}


@pytest.fixture
def outsider(make_user):
    bob = make_user(username="bob", first_name="Bob", phone_number="+5531977770004")
    create_family(name="Outra", admin=bob)
    return bob


@pytest.fixture
def ledger(alice, maria, family, outsider, catalog, make_transaction):
    """Alice (ADMIN), Maria and João (MEMBERs) plus an outsider family; one soft-deleted row."""
    t = make_transaction
    d, p, pp = today(), month_start(1), month_start(2)
    cat = catalog
    # Alice — current month: income 3000; transporte 800, alimentação 200, outros 50 (new).
    t(
        user=alice,
        category=cat["salario"],
        type="INCOME",
        amount="3000.00",
        transaction_date=d,
        description="salário",
    )
    t(
        user=alice,
        category=cat["transporte"],
        amount="800.00",
        transaction_date=d,
        description="gasolina",
    )
    t(user=alice, category=cat["alimentacao"], amount="200.00", transaction_date=d)
    t(user=alice, category=cat["outros_gastos"], amount="50.00", transaction_date=d)
    deleted = t(user=alice, category=cat["transporte"], amount="9999.00", transaction_date=d)
    soft_delete_transaction(actor=alice, transaction_id=deleted.pk)
    # Alice — previous month: income 2500; transporte 500, alimentação 300.
    t(user=alice, category=cat["salario"], type="INCOME", amount="2500.00", transaction_date=p)
    t(user=alice, category=cat["transporte"], amount="500.00", transaction_date=p)
    t(user=alice, category=cat["alimentacao"], amount="300.00", transaction_date=p)
    # Alice — two months ago: alimentação 100.
    t(user=alice, category=cat["alimentacao"], amount="100.00", transaction_date=pp)
    # Maria — current: income 1000, alimentação 420 ("mercado"); previous: alimentação 100.
    t(user=maria, category=cat["salario"], type="INCOME", amount="1000.00", transaction_date=d)
    t(
        user=maria,
        category=cat["alimentacao"],
        amount="420.00",
        transaction_date=d,
        description="mercado",
    )
    t(user=maria, category=cat["alimentacao"], amount="100.00", transaction_date=p)
    # João — only expenses (zero income): transporte 310 current, 90 previous.
    t(user=family["joao"], category=cat["transporte"], amount="310.00", transaction_date=d)
    t(user=family["joao"], category=cat["transporte"], amount="90.00", transaction_date=p)
    # Outsider — never visible.
    t(user=outsider, category=cat["alimentacao"], amount="7777.00", transaction_date=d)
    return {"deleted": deleted}


# --- PERIOD_SUMMARY -------------------------------------------------------------------------


def test_summary_self(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY", scope="SELF"))
    record = deliver(client, "me dê um resumo financeiro desse mês")
    assert reply(evolution) == "\n".join(
        [
            f"Resumo {period_text(*month())}:",
            "Receitas: R$ 3.000,00 (1)",
            "Despesas: R$ 1.050,00 (3)",  # soft-deleted 9.999 ignored
            "Saldo: R$ 1.950,00",
            "Despesas/receitas: 35,0%",
            "Economia: 65,0%",
            "Gasto médio: R$ 350,00",
            "Maior gasto: Gasolina — R$ 800,00",
            "Maior receita: Salário — R$ 3.000,00",
            "Principais categorias: Transporte R$ 800,00; Alimentação R$ 200,00; "
            "Outros gastos R$ 50,00",
        ]
    )
    assert "9.999" not in reply(evolution)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "")


def test_summary_family_admin(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY", scope="FAMILY"))
    deliver(client, "me dê um resumo financeiro da minha família")
    text = reply(evolution)
    assert text.startswith(f"Resumo da família {period_text(*month())}:")
    assert "Receitas: R$ 4.000,00 (2)" in text
    assert "Despesas: R$ 1.780,00 (5)" in text
    assert "Saldo: R$ 2.220,00" in text
    assert "Despesas/receitas: 44,5%" in text
    assert "7.777" not in text and "9.999" not in text


def test_summary_family_member_admin(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY", scope="FAMILY_MEMBER", target_member_name="Maria"))
    deliver(client, "me dê um resumo da Maria")
    assert reply(evolution) == "\n".join(
        [
            f"Resumo de Maria {period_text(*month())}:",
            "Receitas: R$ 1.000,00 (1)",
            "Despesas: R$ 420,00 (1)",
            "Saldo: R$ 580,00",
            "Despesas/receitas: 42,0%",
            "Economia: 58,0%",
            "Gasto médio: R$ 420,00",
            "Maior gasto: Mercado — R$ 420,00",
            "Maior receita: Salário — R$ 1.000,00",
            "Principais categorias: Alimentação R$ 420,00",
        ]
    )


def test_summary_zero_income_has_no_percentages(client, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY"))
    deliver(client, "me dê um resumo desse mês", remote_jid=JOAO_JID)
    text = reply(evolution)
    assert "Receitas: R$ 0,00 (0)" in text
    assert "Saldo: -R$ 310,00" in text
    assert "%" not in text and "Maior receita" not in text


def test_summary_negative_balance_and_ratio_above_100(
    client, alice, catalog, ai, evolution, make_transaction
):
    d = today()
    make_transaction(
        user=alice, category=catalog["salario"], type="INCOME", amount="100", transaction_date=d
    )
    make_transaction(user=alice, category=catalog["alimentacao"], amount="300", transaction_date=d)
    ai.set(query(query_kind="PERIOD_SUMMARY"))
    deliver(client, "qual porcentagem da minha renda eu gastei esse mês?")
    text = reply(evolution)
    assert "Saldo: -R$ 200,00" in text
    assert "Despesas/receitas: 300,0%" in text
    assert "Economia: -200,0%" in text


def test_summary_without_data(client, alice, catalog, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY"))
    deliver(client, "resumo do mês")
    assert (
        reply(evolution) == "Não há movimentações suficientes nesse período para montar o resumo."
    )


# --- MONTHLY_TREND --------------------------------------------------------------------------


def test_monthly_trend_expense_default_six_months(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", trend_metric="EXPENSE"))
    deliver(client, "como meus gastos evoluíram?")
    assert reply(evolution) == "\n".join(
        [
            "Gastos por mês (últimos 6 meses):",
            *trend_lines({2: "R$ 100,00", 1: "R$ 800,00", 0: "R$ 1.050,00"}, 6),
            "Média mensal: R$ 325,00",
        ]
    )


def test_monthly_trend_income_three_months(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", trend_metric="INCOME", months=3))
    deliver(client, "como minhas receitas evoluíram nos últimos 3 meses?")
    assert reply(evolution) == "\n".join(
        [
            "Receitas por mês (últimos 3 meses):",
            *trend_lines({1: "R$ 2.500,00", 0: "R$ 3.000,00"}, 3),
            "Média mensal: R$ 1.833,33",
        ]
    )


def test_monthly_trend_months_override_start_date_and_context_matches_effective_window(
    client, alice, ledger, ai, evolution
):
    """A stray one-day/contradictory start date must not narrow or reject a monthly window."""
    ai.set(
        query(
            query_kind="MONTHLY_TREND",
            trend_metric="EXPENSE",
            months=3,
            start_date=(today() + timedelta(days=1)).isoformat(),
            end_date=today().isoformat(),
        )
    )
    record = deliver(client, "como meus gastos evoluíram nos últimos 3 meses?")

    context = ConversationContext.objects.get(user=alice).last_query
    assert (context["start_date"], context["end_date"]) == (
        month_start(2).isoformat(),
        today().isoformat(),
    )
    assert reply(evolution).startswith("Gastos por mês (últimos 3 meses):")
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "")


def test_monthly_trend_balance(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", trend_metric="BALANCE", months=3))
    deliver(client, "como meu saldo evoluiu?")
    assert reply(evolution) == "\n".join(
        [
            "Saldo por mês (últimos 3 meses):",
            *trend_lines({2: "-R$ 100,00", 1: "R$ 1.700,00", 0: "R$ 1.950,00"}, 3),
            "Média mensal: R$ 1.183,33",
        ]
    )


def test_monthly_trend_metric_from_transaction_type(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", transaction_type="INCOME", months=2))
    deliver(client, "como evoluíram minhas entradas?")
    assert reply(evolution).startswith("Receitas por mês (últimos 2 meses):")


def test_monthly_trend_twelve_months(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", months=12))
    deliver(client, "gastos dos últimos 12 meses mês a mês")
    lines = reply(evolution).split("\n")
    assert lines[0] == "Gastos por mês (últimos 12 meses):"
    assert len(lines) == 14  # title + 12 months + average


@pytest.mark.parametrize("months", [13, 24, 1000])
def test_monthly_trend_above_maximum_is_capped(client, alice, ledger, ai, evolution, months):
    ai.set(query(query_kind="MONTHLY_TREND", months=months))
    record = deliver(client, f"gastos dos últimos {months} meses")
    lines = reply(evolution).split("\n")
    assert lines[0] == "Gastos por mês (últimos 12 meses; máximo permitido):"
    assert len(lines) == 14
    context = ConversationContext.objects.get(user=alice).last_query
    assert context["months"] == 12
    record.refresh_from_db()
    assert record.status == Status.PROCESSED


@pytest.mark.parametrize("months", [0, -3])
def test_monthly_trend_invalid_months_use_default(client, alice, ledger, ai, evolution, months):
    ai.set(query(query_kind="MONTHLY_TREND", months=months))
    deliver(client, "como meus gastos evoluíram?")
    assert reply(evolution).startswith("Gastos por mês (últimos 6 meses):")


def test_monthly_trend_follow_up_metric(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", trend_metric="EXPENSE"))
    deliver(client, "como meus gastos evoluíram?", "M1")
    ai.set(query(follow_up=True, query_kind="MONTHLY_TREND", trend_metric="INCOME", months=6))
    deliver(client, "e minhas receitas?", "M2")
    previous = ai.call_args.args[1].previous_query
    assert (previous["query_kind"], previous["trend_metric"], previous["months"]) == (
        "MONTHLY_TREND",
        "EXPENSE",
        6,
    )
    assert previous["scope"] == "SELF"
    assert reply(evolution).startswith("Receitas por mês (últimos 6 meses):")


def test_family_monthly_trend(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", scope="FAMILY", months=3))
    deliver(client, "como os gastos da minha família evoluíram nos últimos 3 meses?")
    assert reply(evolution) == "\n".join(
        [
            "Gastos da família por mês (últimos 3 meses):",
            *trend_lines({2: "R$ 100,00", 1: "R$ 990,00", 0: "R$ 1.780,00"}, 3),
            "Média mensal: R$ 956,67",
        ]
    )


# --- CATEGORY_TREND -------------------------------------------------------------------------


def trend_header(kind, who=""):
    return f"{kind} de gastos{who} ({period_text(*month())} x {period_text(*prev_month())}):"


def test_category_trend_increase(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="CATEGORY_TREND", trend_direction="INCREASE"))
    deliver(client, "onde meus gastos aumentaram em relação ao mês passado?")
    assert reply(evolution) == "\n".join(
        [
            trend_header("Maiores aumentos"),
            "1. Transporte: +R$ 300,00 (+60,0%)",
            "2. Outros gastos: R$ 50,00; sem movimento no período anterior",
        ]
    )


def test_category_trend_decrease(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="CATEGORY_TREND", trend_direction="DECREASE"))
    deliver(client, "onde eu gastei menos?")
    assert reply(evolution) == "\n".join(
        [trend_header("Maiores reduções"), "1. Alimentação: -R$ 100,00 (-33,3%)"]
    )


def test_category_trend_all_and_default_direction(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="CATEGORY_TREND"))
    deliver(client, "como variaram meus gastos por categoria?")
    assert reply(evolution) == "\n".join(
        [
            trend_header("Variações"),
            "1. Transporte: +R$ 300,00 (+60,0%)",
            "2. Alimentação: -R$ 100,00 (-33,3%)",
            "3. Outros gastos: R$ 50,00; sem movimento no período anterior",
        ]
    )
    assert ConversationContext.objects.get(user=alice).last_query["trend_direction"] == "ALL"


def test_category_trend_nothing_in_direction(client, maria, ledger, ai, evolution):
    ai.set(query(query_kind="CATEGORY_TREND", trend_direction="DECREASE"))
    deliver(client, "onde eu gastei menos?", remote_jid=MARIA_JID)
    previous = period_text(*prev_month()).removeprefix("em ")
    assert reply(evolution) == (
        f"Nenhuma categoria de gastos diminuiu {period_text(*month())} em relação a {previous}."
    )


def test_category_trend_follow_up_direction(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="CATEGORY_TREND", trend_direction="INCREASE"))
    deliver(client, "onde meus gastos aumentaram?", "M1")
    ai.set(query(follow_up=True, query_kind="CATEGORY_TREND", trend_direction="DECREASE"))
    deliver(client, "e onde diminuíram?", "M2")
    previous = ai.call_args.args[1].previous_query
    assert (previous["query_kind"], previous["trend_direction"]) == ("CATEGORY_TREND", "INCREASE")
    assert reply(evolution).startswith("Maiores reduções de gastos")


def test_category_trend_member_of_family(client, alice, ledger, ai, evolution):
    ai.set(
        query(
            query_kind="CATEGORY_TREND",
            trend_direction="INCREASE",
            scope="FAMILY_MEMBER",
            target_member_name="Maria",
        )
    )
    deliver(client, "onde os gastos da Maria aumentaram?")
    assert reply(evolution) == "\n".join(
        [trend_header("Maiores aumentos", " de Maria"), "1. Alimentação: +R$ 320,00 (+320,0%)"]
    )


# --- FAMILY_MEMBER context ------------------------------------------------------------------


def test_member_summary_follow_up_and_switch(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY", scope="FAMILY_MEMBER", target_member_name="maria"))
    deliver(client, "me dê um resumo da Maria esse mês", "M1")
    assert reply(evolution).startswith(f"Resumo de Maria {period_text(*month())}:")

    start, end = prev_month()
    ai.set(
        query(
            follow_up=True,
            query_kind="PERIOD_SUMMARY",
            scope="FAMILY_MEMBER",
            target_member_name="Maria",
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
    )
    deliver(client, "e mês passado?", "M2")
    previous = ai.call_args.args[1].previous_query
    assert (previous["query_kind"], previous["scope"], previous["target_member_name"]) == (
        "PERIOD_SUMMARY",
        "FAMILY_MEMBER",
        "Maria",  # resolved name, not the raw "maria"
    )
    text = reply(evolution)
    assert text.startswith(f"Resumo de Maria {period_text(start, end)}:")
    assert "Despesas: R$ 100,00 (1)" in text

    ai.set(
        query(
            follow_up=True,
            query_kind="PERIOD_SUMMARY",
            scope="FAMILY_MEMBER",
            target_member_name="João",
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
    )
    deliver(client, "e do João?", "M3")
    assert reply(evolution).startswith(f"Resumo de João {period_text(start, end)}:")
    assert "Despesas: R$ 90,00 (1)" in reply(evolution)


def test_role_loss_after_context_denies_follow_up(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY", scope="FAMILY_MEMBER", target_member_name="Maria"))
    deliver(client, "me dê um resumo da Maria", "M1")
    FamilyMember.objects.filter(user=alice).update(role="MEMBER")
    ai.set(
        query(
            follow_up=True,
            query_kind="PERIOD_SUMMARY",
            scope="FAMILY_MEMBER",
            target_member_name="Maria",
        )
    )
    record = deliver(client, "e mês passado?", "M2")
    record.refresh_from_db()
    assert record.error_code == "ADMIN_ONLY"
    assert "R$" not in reply(evolution)


# --- Security -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proposal",
    [
        {"query_kind": "PERIOD_SUMMARY", "scope": "FAMILY"},
        {"query_kind": "MONTHLY_TREND", "scope": "FAMILY"},
        {"query_kind": "MONTHLY_TREND", "scope": "FAMILY", "trend_metric": "BALANCE"},
        {"query_kind": "CATEGORY_TREND", "scope": "FAMILY", "trend_direction": "INCREASE"},
        {"query_kind": "PERIOD_SUMMARY", "scope": "FAMILY_MEMBER", "target_member_name": "Alice"},
        {"query_kind": "MONTHLY_TREND", "scope": "FAMILY_MEMBER", "target_member_name": "João"},
        {"query_kind": "CATEGORY_TREND", "scope": "FAMILY_MEMBER", "target_member_name": "Alice"},
        # A named person is never the sender's own data, even when the model says SELF.
        {"query_kind": "PERIOD_SUMMARY", "scope": "SELF", "target_member_name": "Alice"},
    ],
)
def test_member_cannot_read_family_analytics(client, maria, ledger, ai, evolution, proposal):
    ai.set(query(**proposal))
    record = deliver(client, "resumo da família", remote_jid=MARIA_JID)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "ADMIN_ONLY")
    assert (
        reply(evolution) == "Essa consulta está disponível apenas para o administrador da família."
    )
    assert "R$" not in reply(evolution)


@pytest.mark.parametrize("name", ["Bob", "+5531977770004", "Zé"])
def test_external_target_member_not_found(client, alice, ledger, ai, evolution, name):
    ai.set(query(query_kind="PERIOD_SUMMARY", scope="FAMILY_MEMBER", target_member_name=name))
    record = deliver(client, f"resumo do {name}")
    record.refresh_from_db()
    assert record.error_code == "MEMBER_NOT_FOUND"
    assert "7.777" not in reply(evolution)


def test_manipulated_context_grants_nothing(client, maria, ledger, ai, evolution):
    """A stored context claiming FAMILY (as if tampered) is semantics only: re-authorized."""
    ConversationContext.objects.create(
        user=maria,
        last_query={"scope": "FAMILY", "query_kind": "PERIOD_SUMMARY", "role": "ADMIN"},
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    ai.set(query(follow_up=True, query_kind="PERIOD_SUMMARY", scope="FAMILY"))
    record = deliver(client, "e da família?", remote_jid=MARIA_JID)
    record.refresh_from_db()
    assert record.error_code == "ADMIN_ONLY"
    assert "R$" not in reply(evolution)


def test_member_own_analytics_work(client, maria, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY", scope="SELF"))
    deliver(client, "me dê um resumo desse mês", remote_jid=MARIA_JID)
    text = reply(evolution)
    assert text.startswith(f"Resumo {period_text(*month())}:")
    assert "Despesas: R$ 420,00 (1)" in text and "3.000" not in text


# --- Pipeline side effects ------------------------------------------------------------------


@pytest.mark.parametrize(
    "proposal",
    [
        {"query_kind": "PERIOD_SUMMARY"},
        {"query_kind": "MONTHLY_TREND", "months": 12, "trend_metric": "BALANCE"},
        {"query_kind": "CATEGORY_TREND", "trend_direction": "ALL"},
        # Mutation-looking fields in a query proposal are ignored.
        {"query_kind": "PERIOD_SUMMARY", "amount": "1.00", "description": "x"},
    ],
)
def test_analytics_one_ai_call_no_mutation(client, alice, ledger, ai, evolution, proposal):
    before = snapshot()
    ai.set(query(**proposal))
    record = deliver(client, "análise")
    assert ai.call_count == 1
    assert evolution.call_count == 1
    assert snapshot() == before
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "")


def test_reply_failure_is_not_recomputed(client, alice, ledger, ai, evolution):
    before = snapshot()
    ai.set(query(query_kind="PERIOD_SUMMARY"))
    evolution.side_effect = EvolutionHTTPError(500, "down")
    record = deliver(client, "me dê um resumo financeiro desse mês")
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "REPLY_SEND_FAILED")

    deliver(client, "me dê um resumo financeiro desse mês")  # redelivery
    handle_ready_message(record)  # direct re-run
    assert ai.call_count == 1
    assert evolution.call_count == 1
    assert snapshot() == before
