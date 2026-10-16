"""Regression (closed beta): a member named in a CREATE never becomes the transaction owner.

"Curso da Ayumi" was refused as if it were a booking for Ayumi. Rules under test:
- CREATE always belongs to the sender; names are beneficiaries/description;
- explicit booking for someone else is refused (model flag OR Django's text check), never
  recorded for that person nor silently for the sender;
- family reads (ADMIN) and MEMBER isolation are unchanged.
"""

from decimal import Decimal

import pytest

from apps.families.selectors import other_member_names
from apps.families.services import add_family_member, create_family
from apps.transactions.models import Transaction
from apps.whatsapp import messages
from common.dates import today_local
from common.formatting import describe_period

from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db

MARIA_JID = "5521988887777@s.whatsapp.net"  # maria (fictitious, MEMBER)


@pytest.fixture
def family(alice, maria, make_user):
    alice.first_name = "Alice"
    alice.save()
    ayumi = make_user(
        username="ayumi", first_name="Ayumi", last_name="Tanaka", phone_number="+5531977770009"
    )
    fam = create_family(name="Família DEV", admin=alice)
    add_family_member(family=fam, user=maria)
    add_family_member(family=fam, user=ayumi)
    return {"family": fam, "ayumi": ayumi}


def reply(evolution):
    return evolution.call_args.args[1]


def create(amount, description, **fields):
    return interp(
        intent="CREATE_TRANSACTION",
        transaction_type="EXPENSE",
        amount=amount,
        description=description,
        **fields,
    )


# Old model behaviour (the bug) and the corrected one: neither may change the owner.
NAMED_HINTS = [
    dict(scope="FAMILY_MEMBER", target_member_name="Ayumi"),
    dict(scope="SELF"),
]

BENEFICIARY_MESSAGES = [
    ("Curso da Ayumi, 300 reais", "300", "Curso da Ayumi"),
    ("Comprei presente para Ayumi por 150", "150", "presente para Ayumi"),
    ("Comprei um presente pra Ayumi por 150", "150", "presente pra Ayumi"),
    ("Paguei a mensalidade da faculdade da Ayumi, 500", "500", "mensalidade da faculdade"),
    ("Paguei 80 do salão da Ayumi", "80", "salão da Ayumi"),
]


@pytest.mark.parametrize("hints", NAMED_HINTS)
@pytest.mark.parametrize("text, amount, description", BENEFICIARY_MESSAGES)
def test_admin_beneficiary_mention_creates_for_sender(
    client, alice, family, catalog, ai, evolution, text, amount, description, hints
):
    ai.set(create(amount, description, **hints))
    record = deliver(client, text)

    record.refresh_from_db()
    txn = Transaction.objects.get()
    assert txn.user == alice
    assert txn.amount == Decimal(amount)
    assert txn.description == description
    assert record.error_code == ""
    assert record.transaction == txn
    assert not Transaction.objects.filter(user=family["ayumi"]).exists()


@pytest.mark.parametrize("hints", NAMED_HINTS)
def test_member_beneficiary_mention_creates_for_sender(
    client, maria, family, catalog, ai, evolution, hints
):
    ai.set(create("300", "Curso da Ayumi", **hints))
    deliver(client, "Curso da Ayumi, 300 reais", remote_jid=MARIA_JID)

    txn = Transaction.objects.get()
    assert txn.user == maria
    assert not Transaction.objects.filter(user=family["ayumi"]).exists()


def test_family_scope_on_create_still_belongs_to_sender(client, alice, family, catalog, ai):
    """``scope`` never decides ownership of a creation (it is a read hint only)."""
    ai.set(create("200", "mercado", scope="FAMILY"))
    deliver(client, "gastamos 200 no mercado")
    assert Transaction.objects.get().user == alice


DELEGATION_MESSAGES = [
    "Registra 100 para Ayumi",
    "Registra 100 de mercado para Ayumi",
    "registra 100 de mercado pra ayumi",
    "Lança 50 reais na conta da Ayumi",
    "Ayumi gastou 100 no mercado",
    "Ayumi Tanaka recebeu 200",
]


def assert_refused(record, evolution):
    record.refresh_from_db()
    assert record.status == "PROCESSED"
    assert record.error_code == "FORBIDDEN_OTHER_ACCOUNT"
    assert record.transaction is None
    assert reply(evolution) == messages.OWN_ACCOUNT_ONLY
    assert not Transaction.objects.exists()


@pytest.mark.parametrize("text", DELEGATION_MESSAGES)
def test_delegation_flagged_by_model_is_refused(
    client, alice, family, catalog, ai, evolution, text
):
    ai.set(create("100", "mercado", for_another_person=True, target_member_name="Ayumi"))
    assert_refused(deliver(client, text), evolution)


@pytest.mark.parametrize("text", DELEGATION_MESSAGES)
def test_delegation_missed_by_model_is_refused_by_django(
    client, alice, family, catalog, ai, evolution, text
):
    """Defense in depth: the model says SELF/no flag; Django's text check still refuses."""
    ai.set(create("100", "mercado", scope="SELF"))
    assert_refused(deliver(client, text), evolution)


def test_member_delegation_is_refused(client, maria, family, catalog, ai, evolution):
    ai.set(create("100", "mercado"))
    assert_refused(deliver(client, "Registra 100 para Ayumi", remote_jid=MARIA_JID), evolution)


def test_flag_refuses_even_without_family(client, alice, catalog, ai, evolution):
    ai.set(create("100", "mercado", for_another_person=True))
    assert_refused(deliver(client, "Registra 100 para a Joana"), evolution)


def test_plain_create_without_name_still_works(client, alice, family, catalog, ai, evolution):
    ai.set(
        create(
            "10",
            "gasolina",
            category_id=catalog["transporte"].pk,
            subcategory_id=catalog["combustivel"].pk,
        )
    )
    record = deliver(client, "10 reais de gasolina")
    record.refresh_from_db()
    txn = Transaction.objects.get()
    assert (txn.user, txn.amount, txn.category) == (alice, Decimal("10.00"), catalog["transporte"])
    assert record.error_code == ""


# --- Reads are unchanged ----------------------------------------------------------------------


@pytest.fixture
def ayumi_spending(family, catalog, make_transaction):
    make_transaction(
        user=family["ayumi"],
        category=catalog["alimentacao"],
        amount="300.00",
        transaction_date=today_local(),
    )


def member_query():
    return interp(
        intent="QUERY_TRANSACTIONS",
        query_kind="TOTAL",
        transaction_type="EXPENSE",
        scope="FAMILY_MEMBER",
        target_member_name="Ayumi",
    )


def test_admin_can_still_query_member(client, alice, ayumi_spending, ai, evolution):
    ai.set(member_query())
    deliver(client, "quanto a Ayumi gastou esse mês?")
    today = today_local()
    period = describe_period(today.replace(day=1), today, today)
    assert reply(evolution) == f"Ayumi Tanaka gastou R$ 300,00 {period}."


def test_member_still_cannot_query_member(client, maria, ayumi_spending, ai, evolution):
    ai.set(member_query())
    record = deliver(client, "quanto a Ayumi gastou esse mês?", remote_jid=MARIA_JID)
    record.refresh_from_db()
    assert reply(evolution) == messages.ADMIN_ONLY
    assert "300" not in reply(evolution)
    assert Transaction.objects.count() == 1


# --- Selector ---------------------------------------------------------------------------------


def test_other_member_names_excludes_sender_and_other_families(alice, family, make_user):
    outsider = make_user(username="carla", first_name="Carla", phone_number="+5531977770005")
    create_family(name="Outra", admin=outsider)
    assert other_member_names(user=alice) == {"maria", "ayumi", "ayumi tanaka"}
    assert other_member_names(user=outsider) == set()
