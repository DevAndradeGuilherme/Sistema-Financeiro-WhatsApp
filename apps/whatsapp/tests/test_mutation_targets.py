"""Regression (closed beta): UPDATE/UNDO must hit exactly the transaction the user described.

Observed bugs: "desfaça a entrada de 50" / "desfaça a entrada do dia 07/10" / "desfaça todas
as entradas" soft-deleted the newest expense; "errado, recebi somente 1.050,41" created a new
income. Rules under test (docs/DOMAIN.md §5):
- every stated criterion (type, amount, transaction_date/period, category, description) is
  applied in the sender's own active data; dates are ``transaction_date``, never ``created_at``;
- 0 candidates → nothing changes; 2+ → asks; "the latest" only when the text says "último";
- criteria present in the text but dropped by the model → asks (never a broader search);
- a correction never creates; it edits only an unambiguous, recent context;
- several transactions → confirmation of the exact set ("sim"), with expiry; any other
  message cancels it; nothing widens permissions (MEMBER/ADMIN edit only their own).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.ai.contract import TransactionReference, TransactionUpdates
from apps.families.services import add_family_member, create_family
from apps.transactions.models import Transaction, TransactionChange
from apps.transactions.services import update_transaction
from apps.whatsapp import messages
from apps.whatsapp.models import InboundMessage, PendingAction
from common.dates import today_local
from common.formatting import MONTHS

from .test_audio import download, post_audio, transcribe  # noqa: F401 (fixtures)
from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db
Action = TransactionChange.Action
MARIA_JID = "5521988887777@s.whatsapp.net"


def today():
    return today_local()


# Relative to today so the 90-day search window never makes the suite date-dependent.
D7 = today() - timedelta(days=8)  # "07/10" of the beta report
D6 = today() - timedelta(days=9)  # "06/10"


def month_name():
    return MONTHS[today().month - 1]


def ddmm(day):
    return f"{day:%d/%m}"


def reply(evolution):
    return evolution.call_args.args[1]


def undo(reference=None, **fields):
    return interp(intent="UNDO_TRANSACTION", reference=reference, **fields)


def update(reference=None, correction=False, **updates):
    return interp(
        intent="UPDATE_TRANSACTION",
        reference=reference,
        updates=TransactionUpdates(**updates),
        correction=correction,
    )


def ref(**fields):
    return TransactionReference(**fields)


def state():
    """Snapshot of every transaction: (id, amount, transaction_date, deleted?)."""
    return {
        (t.pk, t.amount, t.transaction_date, t.deleted_at is not None)
        for t in Transaction.objects.all()
    }


def active_ids():
    return set(Transaction.objects.active().values_list("pk", flat=True))


@pytest.fixture
def ledger(alice, catalog, make_transaction):
    """The beta sequence: two incomes, then several expenses registered after them."""
    t = make_transaction
    income_1050 = t(
        user=alice,
        type="INCOME",
        category=catalog["salario"],
        amount="1050.41",
        transaction_date=D6,
    )
    income_50 = t(
        user=alice,
        type="INCOME",
        category=catalog["outras_receitas"],
        amount="50.00",
        transaction_date=D7,
    )
    expenses = [
        t(user=alice, category=catalog["transporte"], amount="12.00", transaction_date=today()),
        t(user=alice, category=catalog["transporte"], amount="80.00", transaction_date=today()),
        t(
            user=alice,
            category=catalog["alimentacao"],
            amount="34.00",
            description="almoço",
            transaction_date=today(),
        ),
    ]
    return {"income_1050": income_1050, "income_50": income_50, "expenses": expenses}


def assert_only_deleted(*expected):
    deleted = set(Transaction.objects.deleted().values_list("pk", flat=True))
    assert deleted == {txn.pk for txn in expected}


def assert_nothing_changed(before):
    assert state() == before
    assert not TransactionChange.objects.filter(action__in=[Action.DELETE, Action.UNDO]).exists()
    assert not TransactionChange.objects.filter(action=Action.UPDATE).exists()


# --- DELETE by explicit criteria --------------------------------------------------------------


def test_undo_income_by_amount_keeps_newer_expense(client, alice, ledger, ai, evolution):
    """1. "Desfaça a entrada de 50" removes the INCOME of 50, not the newest expense of 34."""
    ai.set(undo(ref(transaction_type="INCOME", amount="50")))
    record = deliver(client, "Desfaça a entrada de 50")

    assert_only_deleted(ledger["income_50"])
    record.refresh_from_db()
    assert (record.intent, record.error_code) == ("UNDO_TRANSACTION", "")
    assert record.transaction == ledger["income_50"]
    assert reply(evolution) == (
        f"Pronto, removi a entrada de R$ 50,00 de {ddmm(D7)} (Outras receitas)."
    )
    change = TransactionChange.objects.get(action=Action.DELETE)
    assert change.origin == "whatsapp:test-instance:MSG1"


def test_undo_income_by_date_uses_transaction_date_not_created_at(
    client, alice, catalog, ledger, ai, evolution, make_transaction
):
    """2. "do dia 07/10" = transaction_date. A newer *registration* for another day is kept."""
    late_registration = make_transaction(  # registered last, but refers to 06/10
        user=alice, type="INCOME", category=catalog["salario"], amount="50.00", transaction_date=D6
    )
    ai.set(undo(ref(transaction_type="INCOME", transaction_date=D7.isoformat())))
    deliver(client, f"Desfaça a entrada do dia {ddmm(D7)}")

    assert_only_deleted(ledger["income_50"])
    late_registration.refresh_from_db()
    assert late_registration.deleted_at is None


def test_undo_by_type_amount_and_date(
    client, alice, catalog, ledger, ai, evolution, make_transaction
):
    """3. type + amount + transaction_date, with another INCOME of 50 on a different day."""
    other_50 = make_transaction(
        user=alice, type="INCOME", category=catalog["salario"], amount="50.00", transaction_date=D6
    )
    ai.set(undo(ref(transaction_type="INCOME", amount="50", transaction_date=D7.isoformat())))
    deliver(client, f"Remova a entrada de R$ 50 do dia {ddmm(D7)}")

    assert_only_deleted(ledger["income_50"])
    other_50.refresh_from_db()
    assert other_50.deleted_at is None


@pytest.mark.parametrize(
    "reference, text, expected",
    [
        (
            ref(transaction_type="INCOME", amount="999"),
            "Desfaça a entrada de 999",
            "Não encontrei uma entrada de R$ 999,00.",
        ),
        (
            ref(transaction_type="INCOME", amount="50", transaction_date=D6.isoformat()),
            f"Desfaça a entrada de 50 do dia {ddmm(D6)}",
            f"Não encontrei uma entrada de R$ 50,00 em {ddmm(D6)}.",
        ),
        (
            ref(transaction_type="EXPENSE", amount="50"),
            "Desfaça o gasto de 50",
            "Não encontrei um gasto de R$ 50,00.",
        ),
    ],
)
def test_filter_without_results_changes_nothing(
    client, alice, ledger, ai, evolution, reference, text, expected
):
    """4. No candidate → nothing is touched (no fallback to a broader search)."""
    before = state()
    ai.set(undo(reference))
    record = deliver(client, text)

    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "NOT_FOUND"
    assert reply(evolution) == expected


def test_two_identical_candidates_change_nothing(
    client, alice, catalog, ledger, ai, evolution, make_transaction
):
    """5. Two INCOMEs of 50 on 07/10 → nothing removed, the user is asked which one."""
    make_transaction(
        user=alice, type="INCOME", category=catalog["salario"], amount="50.00", transaction_date=D7
    )
    before = state()
    ai.set(undo(ref(transaction_type="INCOME", amount="50", transaction_date=D7.isoformat())))
    record = deliver(client, f"Remova a entrada de 50 do dia {ddmm(D7)}")

    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "AMBIGUOUS_REFERENCE"
    text = reply(evolution)
    assert text.startswith(
        "Encontrei mais de uma transação correspondente. Qual delas você quer remover?"
    )
    assert text.count(f"• Entrada de R$ 50,00 em {ddmm(D7)}") == 2


def test_ambiguous_list_is_capped(client, alice, catalog, ai, evolution, make_transaction):
    for _ in range(5):
        make_transaction(
            user=alice, type="INCOME", category=catalog["salario"], amount="10", transaction_date=D7
        )
    ai.set(undo(ref(transaction_type="INCOME", amount="10")))
    deliver(client, "Desfaça a entrada de 10")
    text = reply(evolution)
    assert text.count("• Entrada") == 3
    assert "• …e outros" in text
    assert not Transaction.objects.deleted().exists()


# --- "the latest" only when the text says so --------------------------------------------------


@pytest.fixture
def mixed(alice, catalog, make_transaction):
    """Registered in this order: expense A, income X, expense B, income Y, expense Z (newest)."""
    t = make_transaction
    rows = {}
    for name, kind, category in [
        ("A", "EXPENSE", "alimentacao"),
        ("X", "INCOME", "salario"),
        ("B", "EXPENSE", "transporte"),
        ("Y", "INCOME", "outras_receitas"),
        ("Z", "EXPENSE", "alimentacao"),
    ]:
        rows[name] = t(
            user=alice, type=kind, category=catalog[category], amount="10", transaction_date=D7
        )
    # The newest *registration* is not the newest transaction_date.
    rows["Z"].transaction_date = D6
    rows["Z"].save()
    return rows


def test_undo_last_expense(client, alice, mixed, ai, evolution):
    """6. "Desfaça o último gasto" → the sender's most recently registered EXPENSE."""
    ai.set(undo(ref(last=True, transaction_type="EXPENSE")))
    deliver(client, "Desfaça o último gasto")
    assert_only_deleted(mixed["Z"])


def test_undo_last_income(client, alice, mixed, ai, evolution):
    """7. "Desfaça a última entrada" → the most recently registered INCOME (expense Z kept)."""
    ai.set(undo(ref(last=True, transaction_type="INCOME")))
    deliver(client, "Desfaça a última entrada")
    assert_only_deleted(mixed["Y"])


def test_undo_last_transaction(client, alice, mixed, ai, evolution):
    """8. "Desfaça o último lançamento" → the most recently registered of either type."""
    ai.set(undo(ref(last=True)))
    deliver(client, "Desfaça o último lançamento")
    assert_only_deleted(mixed["Z"])


@pytest.mark.parametrize(
    "proposal",
    [undo(), undo(ref(last=True)), undo(ref(transaction_type="INCOME"))],
    ids=["model-dropped-everything", "model-last-without-type", "model-type"],
)
def test_undo_income_never_removes_an_expense(client, alice, mixed, ai, evolution, proposal):
    """9. "Desfaça a entrada" is not "the latest transaction": a type alone identifies nothing
    and no "último" is in the text → the user is asked which entrada."""
    before = state()
    ai.set(proposal)
    record = deliver(client, "Desfaça a entrada")
    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "TARGET_UNCLEAR"
    assert reply(evolution) == messages.remove_which("INCOME")


def test_undo_income_with_single_candidate_asks(client, alice, catalog, ledger, ai, evolution):
    """Even with exactly one INCOME in the window, "desfaça a entrada" does not pick it."""
    ledger["income_1050"].mark_deleted()
    ledger["income_1050"].save()
    before = state()
    ai.set(undo())  # the model dropped the type; the text still says "entrada"
    record = deliver(client, "Desfaça a entrada")
    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "TARGET_UNCLEAR"


def test_other_users_matching_transaction_is_never_a_candidate(
    client, alice, maria, catalog, ai, evolution, make_transaction
):
    """10. Maria's INCOME 50 on 07/10 matches every filter, but only Alice's data is searched."""
    theirs = make_transaction(
        user=maria, type="INCOME", category=catalog["salario"], amount="50", transaction_date=D7
    )
    ai.set(undo(ref(transaction_type="INCOME", amount="50", transaction_date=D7.isoformat())))
    record = deliver(client, f"Remova a entrada de 50 do dia {ddmm(D7)}")
    theirs.refresh_from_db()
    assert theirs.deleted_at is None
    record.refresh_from_db()
    assert record.error_code == "NOT_FOUND"

    mine = make_transaction(
        user=alice, type="INCOME", category=catalog["salario"], amount="50", transaction_date=D7
    )
    deliver(client, f"Remova a entrada de 50 do dia {ddmm(D7)}", "MSG2")
    assert_only_deleted(mine)  # one own candidate, not "two" counting Maria's


# --- Defense in depth: criteria the model dropped ---------------------------------------------


@pytest.mark.parametrize(
    "proposal, text",
    [
        (undo(), "Desfaça a entrada de 50"),
        (undo(ref(transaction_type="INCOME")), "Desfaça a entrada de 50"),
        (undo(), "Desfaça a entrada do dia 07/10"),
        (undo(ref(transaction_type="INCOME")), "Desfaça a entrada do dia 07/10"),
        (undo(ref(transaction_type="INCOME", amount="50")), "Remova a entrada de 50 de ontem"),
        (undo(ref(last=True)), "Desfaça a entrada de 50"),
        (undo(ref(transaction_type="EXPENSE", amount="50")), "Desfaça a entrada de 50"),
    ],
)
def test_criteria_dropped_by_model_never_degrade_to_latest(
    client, alice, ledger, ai, evolution, proposal, text
):
    before = state()
    ai.set(proposal)
    record = deliver(client, text)
    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "TARGET_UNCLEAR"
    assert reply(evolution) == messages.REMOVE_TARGET_UNCLEAR


def test_undo_last_action_needs_the_text_to_say_so(client, alice, catalog, ai, evolution):
    """Audit P1#1: "desfazer" alone changes nothing; "desfaz a última alteração" keeps the
    V1.1 stack semantics (UPDATE → restored, then CREATE → removed)."""
    ai.set(
        interp(
            intent="CREATE_TRANSACTION",
            transaction_type="EXPENSE",
            amount="10",
            category_id=catalog["transporte"].pk,
        )
    )
    deliver(client, "10 de uber", "M1")
    ai.set(update(amount="15"))
    deliver(client, "muda para 15", "M2")
    ai.set(undo())
    record = deliver(client, "desfazer", "M3")
    txn = Transaction.objects.get()
    assert (txn.amount, txn.deleted_at) == (Decimal("15.00"), None)
    record.refresh_from_db()
    assert record.error_code == "TARGET_UNCLEAR"
    deliver(client, "desfaz a última alteração", "M4")
    txn.refresh_from_db()
    assert (txn.amount, txn.deleted_at) == (Decimal("10.00"), None)
    deliver(client, "desfaz a última alteração", "M5")
    txn.refresh_from_db()
    assert txn.deleted_at is not None


# --- UPDATE / contextual correction -----------------------------------------------------------


def create(kind, amount, category, description=None, transaction_date=None):
    return interp(
        intent="CREATE_TRANSACTION",
        transaction_type=kind,
        amount=amount,
        description=description,
        category_id=category.pk,
        transaction_date=transaction_date,
    )


def test_correction_updates_the_transaction_just_created(client, alice, catalog, ai, evolution):
    """11. "Gastei 100 no mercado" → "Errado, foram 80" updates it; no second transaction."""
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    ai.set(update(correction=True, amount="80"))
    record = deliver(client, "Errado, foram 80", "M2")

    txn = Transaction.objects.get()
    assert txn.amount == Decimal("80.00")
    record.refresh_from_db()
    assert (record.intent, record.error_code, record.transaction) == (
        "UPDATE_TRANSACTION",
        "",
        txn,
    )
    assert reply(evolution) == "Pronto, alterei esse gasto para R$ 80,00."


def test_correction_of_income_is_an_update(client, alice, catalog, ai, evolution):
    """12. "Recebi 1050,41" → "Na verdade foram 950" → update, not create."""
    ai.set(create("INCOME", "1050.41", catalog["salario"], transaction_date=D6.isoformat()))
    deliver(client, f"Recebi 1050,41 dia {ddmm(D6)}", "M1")
    ai.set(update(correction=True, amount="950"))
    deliver(client, "Na verdade foram 950", "M2")

    txn = Transaction.objects.get()
    assert (txn.amount, txn.transaction_date) == (Decimal("950.00"), D6)


def test_correction_with_context_never_creates(client, alice, catalog, ai, evolution):
    """13. "Errado, recebi somente 1050,41" right after registering 1.100 → update, no create."""
    ai.set(create("INCOME", "1100", catalog["salario"]))
    deliver(client, "recebi 1100", "M1")
    ai.set(update(ref(transaction_type="INCOME"), correction=True, amount="1050.41"))
    deliver(client, "Errado, recebi somente 1.050,41", "M2")
    txn = Transaction.objects.get()
    assert txn.amount == Decimal("1050.41")


@pytest.mark.parametrize("flagged", [False, True])
def test_correction_misread_as_create_creates_nothing(
    client, alice, catalog, ai, evolution, flagged
):
    """13/14. If the model proposes CREATE for a corrective text, Django refuses to create."""
    ai.set(create("INCOME", "1100", catalog["salario"]))
    deliver(client, "recebi 1100", "M1")
    proposal = interp(
        intent="CREATE_TRANSACTION",
        transaction_type="INCOME",
        amount="1050.41",
        category_id=catalog["salario"].pk,
        correction=flagged,
    )
    ai.set(proposal)
    record = deliver(client, "Errado, recebi somente 1.050,41", "M2")

    assert Transaction.objects.get().amount == Decimal("1100.00")
    record.refresh_from_db()
    assert record.error_code == "CORRECTION_NOT_CREATED"
    assert reply(evolution) == messages.CORRECTION_NOT_CREATED


@pytest.mark.parametrize(
    "text",
    ["Na verdade foram 950", "Quis dizer 80", "não foi 100, foi 80", "Era 50", "corrige isso: 80"],
)
def test_correction_markers_detected_by_django(client, alice, catalog, ai, evolution, text):
    ai.set(create("EXPENSE", "80", catalog["alimentacao"]))
    record = deliver(client, text)
    assert not Transaction.objects.exists()
    record.refresh_from_db()
    assert record.error_code == "CORRECTION_NOT_CREATED"


def age_ledger(minutes):
    TransactionChange.objects.update(created_at=timezone.now() - timedelta(minutes=minutes))


def test_correction_without_recent_context_asks(client, alice, ledger, ai, evolution):
    """14. Same phrase without a safe context (expired) → asks; zero creates/updates."""
    age_ledger(21)  # older than CONVERSATION_CONTEXT_TTL_MINUTES (20)
    before = state()
    ai.set(update(ref(transaction_type="INCOME"), correction=True, amount="1050.41"))
    record = deliver(client, "Errado, recebi somente 1.050,41")

    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "CORRECTION_TARGET_UNCLEAR"
    assert reply(evolution) == messages.CORRECTION_TARGET_UNCLEAR


def test_correction_after_a_query_asks(client, alice, ledger, ai, evolution):
    """14. The beta case: a wrong *total* was answered, then "Errado, recebi somente 1.050,41".
    The correction is about the answer, not one transaction → asks, creates nothing."""
    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="TOTAL",
            transaction_type="INCOME",
            start_date=D6.isoformat(),
            end_date=today().isoformat(),
        )
    )
    deliver(client, "quanto recebi?", "Q1")
    assert "R$ 1.100,41" in reply(evolution)
    before = state()
    ai.set(update(ref(transaction_type="INCOME"), correction=True, amount="1050.41"))
    record = deliver(client, "Errado, recebi somente 1.050,41", "M2")

    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "CORRECTION_TARGET_UNCLEAR"


@pytest.mark.parametrize(
    "proposal, text",
    [
        # The latest transaction is an EXPENSE but the correction is about money received.
        (update(ref(transaction_type="INCOME"), correction=True, amount="50"), "Errado, recebi 50"),
        (update(correction=True, amount="50"), "Errado, recebi 50"),
        # Several on purpose: one at a time only.
        (update(ref(all=True), amount="5"), "muda todos os gastos para 5"),
    ],
)
def test_ambiguous_correction_changes_nothing(client, alice, ledger, ai, evolution, proposal, text):
    """15. Contextual correction that does not identify one transaction → zero mutations."""
    before = state()
    ai.set(proposal)
    deliver(client, text)
    assert_nothing_changed(before)


def test_correction_after_undo_asks(client, alice, catalog, ai, evolution):
    ai.set(create("EXPENSE", "100", catalog["alimentacao"]))
    deliver(client, "gastei 100", "M1")
    ai.set(undo())
    deliver(client, "desfazer", "M2")
    ai.set(update(correction=True, amount="80"))
    record = deliver(client, "errado, foram 80", "M3")
    record.refresh_from_db()
    assert record.error_code == "CORRECTION_TARGET_UNCLEAR"
    assert not TransactionChange.objects.filter(action=Action.UPDATE).exists()


def test_update_with_explicit_criteria_ignores_context(client, alice, ledger, ai, evolution):
    """Explicit criteria win over context: "a entrada de 50 do dia 07/10 era 40"."""
    ai.set(
        update(
            ref(transaction_type="INCOME", amount="50", transaction_date=D7.isoformat()),
            correction=True,
            amount="40",
        )
    )
    deliver(client, f"Errado, a entrada de 50 do dia {ddmm(D7)} era 40")
    ledger["income_50"].refresh_from_db()
    assert ledger["income_50"].amount == Decimal("40.00")
    assert Transaction.objects.filter(amount=Decimal("34.00")).exists()


# --- Bulk: confirmation of the exact set ------------------------------------------------------


@pytest.mark.parametrize(
    "proposal",
    [
        undo(ref(all=True, transaction_type="INCOME")),
        undo(ref(transaction_type="INCOME")),  # model missed "all": the text says "todas"
        undo(),  # the beta case: the model dropped everything
    ],
)
def test_bulk_request_deletes_nothing_and_asks(client, alice, ledger, ai, evolution, proposal):
    """16. "Desfaça todas as entradas" → zero deletes; the presented set is stored."""
    before = state()
    ai.set(proposal)
    record = deliver(client, "Desfaça todas as entradas")

    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "CONFIRMATION_REQUIRED"
    assert reply(evolution) == (
        "Encontrei 2 receitas, totalizando R$ 1.100,41. Deseja realmente remover todas? "
        "Responda SIM para confirmar ou NÃO para cancelar."
    )
    pending = PendingAction.objects.get(user=alice)
    assert set(pending.transaction_ids) == {
        str(ledger["income_50"].pk),
        str(ledger["income_1050"].pk),
    }
    assert (pending.count, pending.total) == (2, Decimal("1100.41"))
    assert pending.expires_at > timezone.now()


def ask_all_incomes(client, ai, message_id="B1"):
    ai.set(undo(ref(all=True, transaction_type="INCOME")))
    return deliver(client, "Apague todas as entradas", message_id)


@pytest.mark.parametrize("answer", ["sim", "Sim!", "confirmo", "pode apagar", "Sim, pode apagar"])
def test_confirmation_deletes_exactly_the_presented_set(
    client, alice, catalog, ledger, ai, evolution, make_transaction, answer
):
    """17. "sim" deletes the presented set only (an income added afterwards stays)."""
    ask_all_incomes(client, ai)
    newer = make_transaction(  # registered outside the channel after the question
        user=alice, type="INCOME", category=catalog["salario"], amount="7", transaction_date=D7
    )
    ai.reset_mock()
    record = deliver(client, answer, "C1")

    ai.assert_not_called()  # an answer never goes to the model
    assert_only_deleted(ledger["income_50"], ledger["income_1050"])
    newer.refresh_from_db()
    assert newer.deleted_at is None
    record.refresh_from_db()
    assert (record.intent, record.error_code) == ("CONFIRM_ACTION", "")
    assert reply(evolution) == "Pronto, removi 2 receitas, totalizando R$ 1.100,41."
    assert set(
        TransactionChange.objects.filter(action=Action.DELETE).values_list("origin", flat=True)
    ) == {"whatsapp:test-instance:C1"}
    assert not PendingAction.objects.exists()


@pytest.mark.parametrize("answer", ["não", "Nao", "cancela", "n"])
def test_denial_cancels(client, alice, ledger, ai, evolution, answer):
    """18."""
    ask_all_incomes(client, ai)
    before = state()
    record = deliver(client, answer, "C1")
    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "CONFIRMATION_CANCELLED"
    assert reply(evolution) == messages.CONFIRMATION_CANCELLED
    assert not PendingAction.objects.exists()
    deliver(client, "sim", "C2")  # the cancelled question cannot be confirmed later
    assert state() == before


def test_expired_confirmation_does_nothing(client, alice, ledger, ai, evolution):
    """19."""
    ask_all_incomes(client, ai)
    PendingAction.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    before = state()
    record = deliver(client, "sim", "C1")
    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "CONFIRMATION_EXPIRED"
    assert reply(evolution) == messages.CONFIRMATION_EXPIRED
    assert not PendingAction.objects.exists()


def test_yes_without_pending_action_does_nothing(client, alice, ledger, ai, evolution):
    """20."""
    before = state()
    record = deliver(client, "sim")
    ai.assert_not_called()
    assert_nothing_changed(before)
    record.refresh_from_db()
    assert (record.status, record.error_code) == (
        InboundMessage.Status.PROCESSED,
        "NO_PENDING_ACTION",
    )
    assert reply(evolution) == messages.NO_PENDING_ACTION


def test_any_other_message_cancels_the_pending_action(client, alice, ledger, ai, evolution):
    """21. A new request cancels the question: a later "sim" confirms nothing."""
    ask_all_incomes(client, ai)
    ai.set(interp(intent="HELP"))
    deliver(client, "ajuda", "H1")
    assert not PendingAction.objects.exists()
    before = state()
    record = deliver(client, "sim", "C1")
    assert_nothing_changed(before)
    record.refresh_from_db()
    assert record.error_code == "NO_PENDING_ACTION"


def test_new_bulk_request_replaces_the_previous_one(client, alice, catalog, ledger, ai, evolution):
    """21. The second question replaces the first: "sim" removes only the expenses of today."""
    ask_all_incomes(client, ai)
    ai.set(undo(ref(all=True, transaction_type="EXPENSE", transaction_date=today().isoformat())))
    deliver(client, "apague os gastos de hoje", "B2")
    assert reply(evolution).startswith("Encontrei 3 gastos hoje, totalizando R$ 126,00.")
    deliver(client, "sim", "C1")
    assert_only_deleted(*ledger["expenses"])


def test_confirmation_refused_when_the_set_changed(client, alice, ledger, ai, evolution):
    ask_all_incomes(client, ai)
    update_transaction(user=alice, transaction_id=ledger["income_50"].pk, amount="60")
    record = deliver(client, "sim", "C1")
    assert not Transaction.objects.deleted().exists()
    record.refresh_from_db()
    assert record.error_code == "CONFIRMATION_STALE"
    assert not PendingAction.objects.exists()


def test_confirmation_is_per_user(client, alice, maria, ledger, ai, evolution):
    ask_all_incomes(client, ai)
    record = deliver(client, "sim", "C1", remote_jid=MARIA_JID)
    record.refresh_from_db()
    assert record.error_code == "NO_PENDING_ACTION"
    assert not Transaction.objects.deleted().exists()
    assert PendingAction.objects.filter(user=alice).exists()


def test_redelivered_confirmation_runs_once(client, alice, ledger, ai, evolution):
    ask_all_incomes(client, ai)
    deliver(client, "sim", "C1")
    deliver(client, "sim", "C1")  # webhook redelivery
    assert TransactionChange.objects.filter(action=Action.DELETE).count() == 2


@pytest.mark.parametrize(
    "proposal, text, code",
    [
        (undo(ref(all=True)), "apague tudo", "BULK_NEEDS_CRITERIA"),
        (
            undo(ref(all=True, transaction_type="INCOME", amount="999")),
            "apague as entradas de 999",
            "NOT_FOUND",
        ),
    ],
)
def test_bulk_without_criteria_or_results(
    client, alice, ledger, ai, evolution, proposal, text, code
):
    ai.set(proposal)
    record = deliver(client, text)
    record.refresh_from_db()
    assert record.error_code == code
    assert not PendingAction.objects.exists()
    assert not Transaction.objects.deleted().exists()


def test_bulk_by_category_and_period(client, alice, catalog, ledger, ai, evolution):
    ai.set(
        undo(
            ref(
                all=True,
                category_id=catalog["transporte"].pk,
                start_date=today().replace(day=1).isoformat(),
                end_date=today().isoformat(),
            )
        )
    )
    deliver(client, "remova todos os gastos de transporte desse mês")
    assert reply(evolution).startswith(f"Encontrei 2 gastos em Transporte em {month_name()},")
    assert "totalizando R$ 92,00" in reply(evolution)
    deliver(client, "pode apagar", "C1")
    assert_only_deleted(*ledger["expenses"][:2])


def test_bulk_too_many(client, alice, catalog, ai, evolution, make_transaction):
    for _ in range(51):
        make_transaction(user=alice, category=catalog["alimentacao"], transaction_date=D7)
    ai.set(undo(ref(all=True, transaction_type="EXPENSE")))
    record = deliver(client, "apague todos os gastos")
    record.refresh_from_db()
    assert record.error_code == "BULK_TOO_MANY"
    assert not PendingAction.objects.exists()


# --- General guarantees -----------------------------------------------------------------------


def test_soft_delete_hides_from_queries(client, alice, ledger, ai, evolution):
    """22. Removed = soft delete: the row stays, totals ignore it."""
    ai.set(undo(ref(transaction_type="INCOME", amount="50")))
    deliver(client, "Desfaça a entrada de 50", "M1")
    removed = Transaction.objects.get(pk=ledger["income_50"].pk)
    assert removed.deleted_at is not None
    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="TOTAL",
            transaction_type="INCOME",
            start_date=D6.isoformat(),
            end_date=today().isoformat(),
        )
    )
    deliver(client, "quanto recebi?", "Q1")  # 27. queries keep working
    assert "R$ 1.050,41" in reply(evolution)


def test_redelivered_delete_applies_once(client, alice, ledger, ai, evolution):
    """23."""
    ai.set(undo(ref(transaction_type="INCOME", amount="50")))
    deliver(client, "Desfaça a entrada de 50", "M1")
    deliver(client, "Desfaça a entrada de 50", "M1")
    assert TransactionChange.objects.filter(action=Action.DELETE).count() == 1
    assert ai.call_count == 1


@pytest.fixture
def family(alice, maria):
    fam = create_family(name="Família", admin=alice)
    add_family_member(family=fam, user=maria)
    return fam


@pytest.mark.parametrize(
    "proposal",
    [
        undo(ref(transaction_type="INCOME", amount="50")),
        undo(
            ref(transaction_type="INCOME", amount="50"),
            scope="FAMILY_MEMBER",
            target_member_name="Alice",
        ),
        undo(ref(all=True, transaction_type="INCOME")),
        update(ref(transaction_type="INCOME", amount="50"), amount="1"),
    ],
)
def test_member_cannot_mutate_admin_transactions(
    client, alice, maria, family, ledger, ai, evolution, proposal
):
    """24. MEMBER: only own data is ever a candidate."""
    before = state()
    ai.set(proposal)
    deliver(client, "Desfaça todas as entradas de 50", remote_jid=MARIA_JID)
    deliver(client, "sim", "C1", remote_jid=MARIA_JID)
    assert state() == before


@pytest.mark.parametrize(
    "proposal",
    [
        undo(ref(transaction_type="INCOME", amount="77")),
        undo(
            ref(transaction_type="INCOME", amount="77"),
            scope="FAMILY_MEMBER",
            target_member_name="Maria",
        ),
        undo(ref(all=True, transaction_type="INCOME", amount="77"), scope="FAMILY"),
        update(ref(transaction_type="INCOME", amount="77"), amount="1"),
    ],
)
def test_admin_cannot_mutate_member_transactions(
    client, alice, maria, family, catalog, ai, evolution, make_transaction, proposal
):
    """25. ADMIN reads the family but never edits/removes a member's transaction."""
    theirs = make_transaction(
        user=maria, type="INCOME", category=catalog["salario"], amount="77", transaction_date=D7
    )
    ai.set(proposal)
    deliver(client, "Desfaça a entrada de 77 da Maria")
    deliver(client, "sim", "C1")
    theirs.refresh_from_db()
    assert (theirs.amount, theirs.deleted_at) == (Decimal("77.00"), None)


def test_plain_create_still_works(client, alice, catalog, ledger, ai, evolution):
    """26. "Recebi 950 hoje" without corrective markers is a normal CREATE."""
    ai.set(create("INCOME", "950", catalog["salario"]))
    record = deliver(client, "Recebi 950 hoje")
    record.refresh_from_db()
    assert record.error_code == ""
    assert Transaction.objects.filter(amount=Decimal("950.00"), user=alice).exists()


def test_audio_follows_the_same_rules(
    client,
    alice,
    ledger,
    ai,
    evolution,
    download,  # noqa: F811
    transcribe,  # noqa: F811
):
    """28. Audio reaches the same core: dropped criteria → asks; full criteria → exact target."""
    transcribe.say("desfaça a entrada de 50")
    ai.set(undo())
    record = post_audio(client, message_id="A1")
    record.refresh_from_db()
    assert record.error_code == "TARGET_UNCLEAR"
    assert not Transaction.objects.deleted().exists()

    ai.set(undo(ref(transaction_type="INCOME", amount="50")))
    post_audio(client, message_id="A2")
    assert_only_deleted(ledger["income_50"])

    transcribe.say("apague todas as entradas")
    ai.set(undo(ref(all=True, transaction_type="INCOME")))
    post_audio(client, message_id="A3")
    transcribe.say("sim")
    post_audio(client, message_id="A4")
    assert_only_deleted(ledger["income_50"], ledger["income_1050"])


# --- Beta scenario ----------------------------------------------------------------------------


def test_beta_sequence(client, alice, ledger, ai, evolution):
    """Incomes 1050.41 (06/10) and 50 (07/10), expenses registered after, a query, then the
    removals exactly as typed in the beta."""
    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="LIST",
            transaction_type="INCOME",
        )
    )
    deliver(client, "quais foram minhas receitas?", "Q1")
    assert "R$ 1.050,41" in reply(evolution) and "R$ 50,00" in reply(evolution)

    ai.set(undo(ref(transaction_type="INCOME", amount="50")))
    deliver(client, "Desfaça a entrada de 50", "M1")
    assert_only_deleted(ledger["income_50"])

    ai.set(undo(ref(transaction_type="INCOME", transaction_date=D6.isoformat())))
    deliver(client, f"Desfaça a entrada do dia {ddmm(D6)}", "M2")
    assert_only_deleted(ledger["income_50"], ledger["income_1050"])
    assert all(t.deleted_at is None for t in ledger["expenses"])
