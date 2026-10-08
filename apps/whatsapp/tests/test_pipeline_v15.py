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
from apps.whatsapp import messages
from apps.whatsapp.client import EvolutionHTTPError
from apps.whatsapp.models import ConversationContext, InboundMessage
from apps.whatsapp.pipeline import handle_ready_message
from common.dates import today_local
from common.formatting import MONTHS, describe_period

from .replies import rows
from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db
Status = InboundMessage.Status

MARIA_JID = "5521988887777@s.whatsapp.net"  # maria (fictitious)
JOAO_JID = "5531977770003@s.whatsapp.net"  # joão (fictitious)


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
    """Expected '<Mês>: R$ x' lines, oldest first (year suffix when the window spans years)."""
    window = [month_start(offset) for offset in reversed(range(months))]
    years = {m.year for m in window}
    lines = []
    for offset, m in zip(reversed(range(months)), window, strict=True):
        label = MONTHS[m.month - 1].capitalize() + (f" de {m.year}" if len(years) > 1 else "")
        lines.append(f"{label}: {values_by_offset.get(offset, 'R$ 0,00')}")
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
    assert reply(evolution).split("\n\n") == [
        # soft-deleted 9.999 ignored
        f"{cap_period(*month())}, entraram R$ 3.000,00 e você gastou R$ 1.050,00.",
        "Seu saldo no período ficou em R$ 1.950,00, com 35,0% da renda comprometida.",
        "A categoria com mais gastos foi Transporte, com R$ 800,00. "
        "O maior gasto foi Gasolina, de R$ 800,00.",
    ]
    assert "9.999" not in reply(evolution)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "")


def test_summary_family_admin(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY", scope="FAMILY"))
    deliver(client, "me dê um resumo financeiro da minha família")
    text = reply(evolution)
    assert text.startswith(
        f"{cap_period(*month())}, entraram R$ 4.000,00 e sua família gastou R$ 1.780,00."
    )
    assert "O saldo da sua família no período ficou em R$ 2.220,00" in text
    assert "44,5% da renda comprometida" in text
    assert "7.777" not in text and "9.999" not in text


def test_summary_family_member_admin(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY", scope="FAMILY_MEMBER", target_member_name="Maria"))
    deliver(client, "me dê um resumo da Maria")
    text = reply(evolution)
    assert text.startswith(
        f"{cap_period(*month())}, entraram R$ 1.000,00 e Maria gastou R$ 420,00."
    )
    assert "O saldo de Maria no período ficou em R$ 580,00" in text
    assert "42,0% da renda comprometida" in text
    assert "A categoria com mais gastos foi Alimentação, com R$ 420,00." in text
    assert "O maior gasto foi Mercado, de R$ 420,00." in text


def test_summary_zero_income_has_no_percentages(client, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY"))
    deliver(client, "me dê um resumo desse mês", remote_jid=JOAO_JID)
    text = reply(evolution)
    assert "você gastou R$ 310,00 e não houve receitas" in text
    assert "Seu saldo no período ficou negativo em R$ 310,00." in text
    assert "%" not in text


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
    assert "ficou negativo em R$ 200,00, com 300,0% da renda comprometida." in text


def test_summary_without_data(client, alice, catalog, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY"))
    deliver(client, "resumo do mês")
    assert reply(evolution) == messages.NO_DATA


# --- MONTHLY_TREND --------------------------------------------------------------------------


def test_monthly_trend_expense_default_six_months(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", trend_metric="EXPENSE"))
    deliver(client, "como meus gastos evoluíram?")
    assert reply(evolution).split("\n\n") == [
        "Nos últimos 6 meses, seus gastos foram:",
        "\n".join(trend_lines({2: "R$ 100,00", 1: "R$ 800,00", 0: "R$ 1.050,00"}, 6)),
        "A média mensal ficou em R$ 325,00.",
    ]


def test_monthly_trend_income_three_months(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", trend_metric="INCOME", months=3))
    deliver(client, "como minhas receitas evoluíram nos últimos 3 meses?")
    text = reply(evolution)
    assert text.startswith("Nos últimos 3 meses, suas receitas foram:")
    assert rows(text) == trend_lines({1: "R$ 2.500,00", 0: "R$ 3.000,00"}, 3)
    assert text.endswith("A média mensal ficou em R$ 1.833,33.")


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
    assert reply(evolution).startswith("Nos últimos 3 meses, seus gastos foram:")
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "")


def test_monthly_trend_balance(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", trend_metric="BALANCE", months=3))
    deliver(client, "como meu saldo evoluiu?")
    text = reply(evolution)
    assert text.startswith("Nos últimos 3 meses, seu saldo foi:")
    assert rows(text) == trend_lines({2: "-R$ 100,00", 1: "R$ 1.700,00", 0: "R$ 1.950,00"}, 3)
    assert text.endswith("A média mensal ficou em R$ 1.183,33.")


def test_monthly_trend_metric_from_transaction_type(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", transaction_type="INCOME", months=2))
    deliver(client, "como evoluíram minhas entradas?")
    assert reply(evolution).startswith("Nos últimos 2 meses, suas receitas foram:")


def test_monthly_trend_twelve_months(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", months=12))
    deliver(client, "gastos dos últimos 12 meses mês a mês")
    text = reply(evolution)
    assert text.startswith("Nos últimos 12 meses, seus gastos foram:")
    assert len(rows(text)) == 12


@pytest.mark.parametrize("months", [13, 24, 1000])
def test_monthly_trend_above_maximum_is_capped(client, alice, ledger, ai, evolution, months):
    ai.set(query(query_kind="MONTHLY_TREND", months=months))
    record = deliver(client, f"gastos dos últimos {months} meses")
    text = reply(evolution)
    assert text.startswith("Nos últimos 12 meses (o máximo que consigo mostrar), seus gastos")
    assert len(rows(text)) == 12
    context = ConversationContext.objects.get(user=alice).last_query
    assert context["months"] == 12
    record.refresh_from_db()
    assert record.status == Status.PROCESSED


@pytest.mark.parametrize("months", [0, -3])
def test_monthly_trend_invalid_months_use_default(client, alice, ledger, ai, evolution, months):
    ai.set(query(query_kind="MONTHLY_TREND", months=months))
    deliver(client, "como meus gastos evoluíram?")
    assert reply(evolution).startswith("Nos últimos 6 meses, seus gastos foram:")


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
    assert reply(evolution).startswith("Nos últimos 6 meses, suas receitas foram:")


def test_family_monthly_trend(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="MONTHLY_TREND", scope="FAMILY", months=3))
    deliver(client, "como os gastos da minha família evoluíram nos últimos 3 meses?")
    text = reply(evolution)
    assert text.startswith("Nos últimos 3 meses, os gastos da sua família foram:")
    assert rows(text) == trend_lines({2: "R$ 100,00", 1: "R$ 990,00", 0: "R$ 1.780,00"}, 3)
    assert text.endswith("A média mensal ficou em R$ 956,67.")


# --- CATEGORY_TREND -------------------------------------------------------------------------


def cap_period(start, end):
    return period_text(start, end).capitalize()


def trend_header(kind, target="nos seus gastos"):
    previous = period_text(*prev_month()).removeprefix("em ")
    return f"{cap_period(*month())}, {kind} {target} em relação a {previous} foram:"


def test_category_trend_increase(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="CATEGORY_TREND", trend_direction="INCREASE"))
    deliver(client, "onde meus gastos aumentaram em relação ao mês passado?")
    text = reply(evolution)
    assert text.startswith(trend_header("os maiores aumentos"))
    assert rows(text) == [
        "Transporte: +R$ 300,00 (+60,0%)",
        "Outros gastos: R$ 50,00; não houve gasto no período anterior",
    ]


def test_category_trend_decrease(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="CATEGORY_TREND", trend_direction="DECREASE"))
    deliver(client, "onde eu gastei menos?")
    text = reply(evolution)
    assert text.startswith(trend_header("as maiores reduções"))
    assert rows(text) == ["Alimentação: -R$ 100,00 (-33,3%)"]


def test_category_trend_all_and_default_direction(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="CATEGORY_TREND"))
    deliver(client, "como variaram meus gastos por categoria?")
    text = reply(evolution)
    assert text.startswith(trend_header("as variações"))
    assert rows(text) == [
        "Transporte: +R$ 300,00 (+60,0%)",
        "Alimentação: -R$ 100,00 (-33,3%)",
        "Outros gastos: R$ 50,00; não houve gasto no período anterior",
    ]
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
    assert reply(evolution).startswith(trend_header("as maiores reduções"))


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
    text = reply(evolution)
    assert text.startswith(trend_header("os maiores aumentos", "nos gastos de Maria"))
    assert rows(text) == ["Alimentação: +R$ 320,00 (+320,0%)"]


# --- FAMILY_MEMBER context ------------------------------------------------------------------


def test_member_summary_follow_up_and_switch(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="PERIOD_SUMMARY", scope="FAMILY_MEMBER", target_member_name="maria"))
    deliver(client, "me dê um resumo da Maria esse mês", "M1")
    assert reply(evolution).startswith(f"{cap_period(*month())}, entraram")
    assert "Maria gastou R$ 420,00" in reply(evolution)

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
    assert text.startswith(cap_period(start, end))
    assert "Maria gastou R$ 100,00" in text

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
    assert reply(evolution).startswith(cap_period(start, end))
    assert "João gastou R$ 90,00" in reply(evolution)


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
    assert text.startswith(cap_period(*month()))
    assert "você gastou R$ 420,00" in text and "3.000" not in text


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
