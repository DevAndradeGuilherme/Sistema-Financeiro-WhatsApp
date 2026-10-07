"""V1.2 through the pipeline: family reads for the ADMIN, isolation for MEMBERs.

The AI mock returns the semantic proposal (scope / target_member_name); Django must decide.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.ai.contract import TransactionReference, TransactionUpdates
from apps.families.models import FamilyMember
from apps.families.services import add_family_member, create_family
from apps.transactions.models import Transaction, TransactionChange
from apps.whatsapp.models import ConversationContext, InboundMessage
from common.dates import today_local
from common.formatting import describe_period

from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db
Status = InboundMessage.Status

ADMIN_JID = "5531999990001@s.whatsapp.net"  # alice (fictitious)
MARIA_JID = "5521988887777@s.whatsapp.net"  # maria (fictitious)


def today():
    return today_local()


def month():
    d = today()
    return d.replace(day=1), d


def prev_month():
    end = today().replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def period_text(start, end):
    return describe_period(start, end, today())


def reply(evolution):
    return evolution.call_args.args[1]


def query(**fields):
    return interp(intent="QUERY_TRANSACTIONS", **fields)


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
def other_family(make_user):
    bob = make_user(username="bob", first_name="Bob", phone_number="+5531977770004")
    carla = make_user(username="carla", first_name="Carla", phone_number="+5531977770005")
    fam = create_family(name="Outra", admin=bob)
    add_family_member(family=fam, user=carla)
    return {"bob": bob, "carla": carla}


@pytest.fixture
def ledger(alice, maria, family, other_family, catalog, make_transaction):
    d = today()
    p_start, _ = prev_month()
    t = make_transaction
    t(
        user=alice,
        category=catalog["transporte"],
        amount="800.00",
        transaction_date=d,
        description="gasolina",
    )
    t(
        user=maria,
        category=catalog["alimentacao"],
        amount="420.00",
        transaction_date=d,
        description="mercado",
    )
    t(user=maria, category=catalog["transporte"], amount="200.00", transaction_date=d)
    t(user=family["joao"], category=catalog["alimentacao"], amount="310.00", transaction_date=d)
    t(user=maria, category=catalog["salario"], type="INCOME", amount="3000.00", transaction_date=d)
    t(user=maria, category=catalog["alimentacao"], amount="100.00", transaction_date=p_start)
    t(
        user=other_family["bob"],
        category=catalog["alimentacao"],
        amount="7777.00",
        transaction_date=d,
    )
    t(
        user=other_family["carla"],
        category=catalog["alimentacao"],
        amount="5555.00",
        transaction_date=d,
    )


# --- ADMIN: family queries ------------------------------------------------------------------


def test_admin_self_query_still_own_data(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE", scope="SELF"))
    deliver(client, "quanto gastei esse mês?")
    assert reply(evolution) == f"Você gastou R$ 800,00 {period_text(*month())}."


def test_family_total(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    record = deliver(client, "quanto minha família gastou esse mês?")
    assert reply(evolution) == f"A família gastou R$ 1.730,00 {period_text(*month())}."
    record.refresh_from_db()
    assert (record.status, record.error_code) == (Status.PROCESSED, "")


def test_family_income_and_balance(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="INCOME", scope="FAMILY"))
    deliver(client, "quanto minha família recebeu esse mês?", "M1")
    assert reply(evolution) == f"A família recebeu R$ 3.000,00 {period_text(*month())}."
    ai.set(query(query_kind="BALANCE", scope="FAMILY"))
    deliver(client, "qual o saldo da minha família?", "M2")
    assert reply(evolution) == (
        f"O saldo da família {period_text(*month())} é R$ 1.270,00 "
        "(entradas R$ 3.000,00; gastos R$ 1.730,00)."
    )


def test_family_breakdown(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="BREAKDOWN", scope="FAMILY"))
    deliver(client, "quanto gastamos por categoria?")
    assert reply(evolution) == (
        f"Gastos da família por categoria {period_text(*month())}:\n"
        "1. Transporte — R$ 1.000,00\n"
        "2. Alimentação — R$ 730,00"
    )


def test_family_top(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="TOP", scope="FAMILY", limit=3))
    deliver(client, "quais foram os maiores gastos da família esse mês?")
    assert reply(evolution) == (
        f"Maiores gastos da família {period_text(*month())}:\n"
        "1. Gasolina (Alice) — R$ 800,00\n"
        "2. Mercado (Maria) — R$ 420,00\n"
        "3. Alimentação (João) — R$ 310,00"
    )


def test_family_compare(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="COMPARE", scope="FAMILY"))
    deliver(client, "compare os gastos da família desse mês com o mês passado")
    assert reply(evolution) == (
        f"A família gastou R$ 1.730,00 {period_text(*month())} e R$ 100,00 "
        f"{period_text(*prev_month())}: R$ 1.630,00 a mais (+1630,0%)."
    )


def test_member_ranking(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="RANKING", scope="FAMILY"))
    deliver(client, "quem gastou mais esse mês?")
    assert reply(evolution) == (
        f"Gastos por pessoa {period_text(*month())}:\n"
        "1. Alice — R$ 800,00\n"
        "2. Maria — R$ 620,00\n"
        "3. João — R$ 310,00"
    )


def test_ranking_without_scope_is_still_family_and_authorized(client, maria, ledger, ai, evolution):
    ai.set(query(query_kind="RANKING", scope=None))
    record = deliver(client, "quanto cada pessoa gastou?", remote_jid=MARIA_JID)
    record.refresh_from_db()
    assert record.error_code == "ADMIN_ONLY"


# --- ADMIN: one member ----------------------------------------------------------------------


def test_member_total_and_category(client, alice, ledger, catalog, ai, evolution):
    ai.set(
        query(
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            scope="FAMILY_MEMBER",
            target_member_name="Maria",
        )
    )
    deliver(client, "quanto a Maria gastou esse mês?", "M1")
    assert reply(evolution) == f"Maria gastou R$ 620,00 {period_text(*month())}."
    ai.set(
        query(
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            scope="FAMILY_MEMBER",
            target_member_name="maria",
            category_id=catalog["alimentacao"].pk,
        )
    )
    deliver(client, "quanto a Maria gastou com alimentação?", "M2")
    assert reply(evolution) == f"Maria gastou R$ 420,00 com Alimentação {period_text(*month())}."


def test_member_top_and_compare(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="TOP", scope="FAMILY_MEMBER", target_member_name="Maria"))
    deliver(client, "quais foram os maiores gastos da Maria?", "M1")
    assert reply(evolution) == (
        f"Maiores gastos de Maria {period_text(*month())}:\n"
        "1. Mercado — R$ 420,00\n"
        "2. Transporte — R$ 200,00"
    )
    ai.set(query(query_kind="COMPARE", scope="FAMILY_MEMBER", target_member_name="Maria"))
    deliver(client, "compare os gastos da Maria", "M2")
    assert reply(evolution).startswith(
        f"Maria gastou R$ 620,00 {period_text(*month())} e R$ 100,00"
    )


def test_name_without_scope_is_treated_as_member_query(client, maria, ledger, ai, evolution):
    """A named person is never the sender's own data, even if the model says SELF."""
    ai.set(
        query(
            query_kind="TOTAL", transaction_type="EXPENSE", scope="SELF", target_member_name="Alice"
        )
    )
    record = deliver(client, "quanto a Alice gastou?", remote_jid=MARIA_JID)
    record.refresh_from_db()
    assert record.error_code == "ADMIN_ONLY"


def test_unknown_or_other_family_member(client, alice, ledger, ai, evolution):
    for index, name in enumerate(("Carla", "Bob", "+5531977770005", "Zé")):
        ai.set(
            query(
                query_kind="TOTAL",
                transaction_type="EXPENSE",
                scope="FAMILY_MEMBER",
                target_member_name=name,
            )
        )
        record = deliver(client, f"quanto {name} gastou?", f"M{index}")
        record.refresh_from_db()
        assert record.error_code == "MEMBER_NOT_FOUND"
        assert reply(evolution) == "Não encontrei esse membro na sua família."
        assert "5.555" not in reply(evolution) and "7.777" not in reply(evolution)


def test_ambiguous_member_name(client, alice, family, ledger, make_user, ai, evolution):
    homonym = make_user(
        username="maria2", first_name="Maria", last_name="Souza", phone_number="+5531977770009"
    )
    add_family_member(family=family["family"], user=homonym)
    ai.set(
        query(
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            scope="FAMILY_MEMBER",
            target_member_name="Maria",
        )
    )
    record = deliver(client, "quanto a Maria gastou?", "M1")
    record.refresh_from_db()
    assert record.error_code == "MEMBER_AMBIGUOUS"
    assert reply(evolution) == (
        "Encontrei mais de um membro chamado Maria na família. Pode informar o nome completo?"
    )
    ai.set(
        query(
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            scope="FAMILY_MEMBER",
            target_member_name="Maria Souza",
        )
    )
    deliver(client, "quanto a Maria Souza gastou?", "M2")
    assert reply(evolution).startswith("Maria Souza gastou R$ 0,00")


# --- MEMBER isolation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "proposal",
    [
        {"query_kind": "TOTAL", "transaction_type": "EXPENSE", "scope": "FAMILY"},
        {
            "query_kind": "TOTAL",
            "transaction_type": "EXPENSE",
            "scope": "FAMILY_MEMBER",
            "target_member_name": "Alice",
        },
        {"query_kind": "BREAKDOWN", "scope": "FAMILY"},
        {"query_kind": "TOP", "scope": "FAMILY"},
        {"query_kind": "COMPARE", "scope": "FAMILY"},
        {"query_kind": "BALANCE", "scope": "FAMILY"},
    ],
)
def test_member_family_queries_denied(client, maria, ledger, ai, evolution, proposal):
    ai.set(query(**proposal))
    record = deliver(client, "consulta da família", remote_jid=MARIA_JID)
    record.refresh_from_db()
    assert record.error_code == "ADMIN_ONLY"
    assert (
        reply(evolution) == "Essa consulta está disponível apenas para o administrador da família."
    )
    assert "R$" not in reply(evolution)


def test_member_self_query_works(client, maria, ledger, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE", scope="SELF"))
    deliver(client, "quanto gastei esse mês?", remote_jid=MARIA_JID)
    assert reply(evolution) == f"Você gastou R$ 620,00 {period_text(*month())}."


def test_user_without_family(client, alice, catalog, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    record = deliver(client, "quanto minha família gastou?")
    record.refresh_from_db()
    assert record.error_code == "NO_FAMILY"
    assert reply(evolution) == "Você ainda não faz parte de uma família cadastrada."


def test_admin_alone_in_family(client, alice, catalog, ai, evolution, make_transaction):
    create_family(name="Solo", admin=alice)
    make_transaction(
        user=alice, category=catalog["transporte"], amount="15", transaction_date=today()
    )
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    deliver(client, "quanto minha família gastou?")
    assert reply(evolution) == f"A família gastou R$ 15,00 {period_text(*month())}."


# --- Mutations on someone else ----------------------------------------------------------------


@pytest.mark.parametrize(
    "proposal, expected",
    [
        (
            dict(
                intent="CREATE_TRANSACTION",
                transaction_type="EXPENSE",
                amount="50",
                scope="FAMILY_MEMBER",
                target_member_name="Maria",
            ),
            "Você só pode registrar transações na sua própria conta.",
        ),
        (
            dict(
                intent="UPDATE_TRANSACTION",
                scope="FAMILY_MEMBER",
                target_member_name="Maria",
                reference=TransactionReference(last=True),
                updates=TransactionUpdates(amount="30"),
            ),
            "Você só pode alterar suas próprias transações.",
        ),
        (
            dict(intent="UNDO_TRANSACTION", scope="FAMILY_MEMBER", target_member_name="João"),
            "Você só pode alterar suas próprias transações.",
        ),
    ],
)
def test_admin_cannot_mutate_for_others(client, alice, ledger, ai, evolution, proposal, expected):
    before = {(t.pk, t.amount, t.deleted_at) for t in Transaction.objects.all()}
    changes_before = TransactionChange.objects.count()
    ai.set(interp(**proposal))
    record = deliver(client, "muda o gasto da Maria")
    record.refresh_from_db()
    assert record.error_code == "FORBIDDEN_OTHER_ACCOUNT"
    assert reply(evolution) == expected
    assert {(t.pk, t.amount, t.deleted_at) for t in Transaction.objects.all()} == before
    assert TransactionChange.objects.count() == changes_before


def test_admin_update_never_reaches_member_even_without_scope(
    client, alice, maria, ledger, ai, evolution
):
    """If the model misses the name, Django still only searches the sender's transactions."""
    maria_ids = set(Transaction.objects.filter(user=maria).values_list("pk", "amount"))
    ai.set(
        interp(
            intent="UPDATE_TRANSACTION",
            reference=TransactionReference(description="mercado"),
            updates=TransactionUpdates(amount="1"),
        )
    )
    record = deliver(client, "muda o gasto do mercado da Maria para 1")
    record.refresh_from_db()
    assert record.error_code == "NOT_FOUND"
    assert set(Transaction.objects.filter(user=maria).values_list("pk", "amount")) == maria_ids


# --- Context ------------------------------------------------------------------------------------


def test_family_follow_up_period(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    deliver(client, "quanto minha família gastou esse mês?", "M1")
    start, end = prev_month()
    ai.set(
        query(
            follow_up=True,
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            scope="FAMILY",
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
    )
    deliver(client, "e mês passado?", "M2")
    assert reply(evolution) == f"A família gastou R$ 100,00 {period_text(start, end)}."
    previous = ai.call_args.args[1].previous_query
    assert (previous["scope"], previous["target_member_name"]) == ("FAMILY", None)


def test_member_follow_up_category_and_switch(client, alice, ledger, catalog, ai, evolution):
    ai.set(
        query(
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            scope="FAMILY_MEMBER",
            target_member_name="Maria",
        )
    )
    deliver(client, "quanto a Maria gastou esse mês?", "M1")
    ai.set(
        query(
            follow_up=True,
            query_kind="TOTAL",
            transaction_type="EXPENSE",
            scope="FAMILY_MEMBER",
            target_member_name="Maria",
            category_id=catalog["alimentacao"].pk,
        )
    )
    deliver(client, "e com alimentação?", "M2")
    assert reply(evolution).startswith("Maria gastou R$ 420,00 com Alimentação")
    assert ai.call_args.args[1].previous_query["target_member_name"] == "Maria"  # resolved name

    ai.set(query(query_kind="TOP", scope="FAMILY_MEMBER", target_member_name="João"))
    deliver(client, "quais foram os maiores gastos do João?", "M3")
    ai.set(
        query(follow_up=True, query_kind="TOP", scope="FAMILY_MEMBER", target_member_name="Maria")
    )
    deliver(client, "e da Maria?", "M4")
    assert reply(evolution).startswith("Maiores gastos de Maria")


def test_context_never_grants_permission_after_role_loss(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    deliver(client, "quanto minha família gastou?", "M1")
    FamilyMember.objects.filter(user=alice).update(role="MEMBER")
    ai.set(query(follow_up=True, query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    record = deliver(client, "e mês passado?", "M2")
    record.refresh_from_db()
    assert record.error_code == "ADMIN_ONLY"


def test_member_context_does_not_become_family(client, maria, ledger, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE"))
    deliver(client, "quanto gastei?", "M1", remote_jid=MARIA_JID)
    assert ConversationContext.objects.get(user=maria).last_query["scope"] == "SELF"
    ai.set(query(follow_up=True, query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    record = deliver(client, "e a família?", "M2", remote_jid=MARIA_JID)
    record.refresh_from_db()
    assert record.error_code == "ADMIN_ONLY"


def test_family_context_cannot_cross_families(client, alice, other_family, ledger, ai, evolution):
    """Bob's (family B) context is never used for Alice; Alice's scope is always her family."""
    ConversationContext.objects.create(
        user=other_family["bob"],
        last_query={"scope": "FAMILY", "query_kind": "TOTAL"},
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    ai.set(query(follow_up=True, query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    record = deliver(client, "e mês passado?")
    record.refresh_from_db()
    assert record.error_code == "NO_CONTEXT"
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    deliver(client, "quanto minha família gastou?", "M2")
    assert "7.777" not in reply(evolution) and "5.555" not in reply(evolution)


def test_expired_family_context(client, alice, ledger, ai, evolution):
    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    deliver(client, "quanto minha família gastou?", "M1")
    ConversationContext.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    ai.set(query(follow_up=True, query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    record = deliver(client, "e mês passado?", "M2")
    record.refresh_from_db()
    assert record.error_code == "NO_CONTEXT"


def test_ai_receives_no_member_list_or_role(client, alice, family, ledger, ai, evolution):
    from apps.ai.prompts import build_instructions

    ai.set(query(query_kind="TOTAL", transaction_type="EXPENSE", scope="FAMILY"))
    deliver(client, "quanto minha família gastou?")
    instructions = build_instructions(ai.call_args.args[1])
    for private in ("Maria", "João", "ADMIN", "+5521988887777", "Família DEV"):
        assert private not in instructions.split("Catalog:")[1]
