"""Regression for the final audit (REJECTED, four P1). Concurrency (P1#3) lives in
``test_reserve_ordering_concurrency.py``.

P1#1 — every date/period the text states is resolved to a value ("dia 5" → that day,
"outubro" → that month) and the model's target must lie inside it; an expression that cannot be
resolved safely → TARGET_UNCLEAR, zero mutation.
P1#2 — UPDATE selects a transaction only by "último" or by a criterion beyond the type that
the text itself states; criteria only the model proposed never choose the target.
P1#4 — an elliptic amount ("80", "ok, 80") after a recent registration whose correction link
was interrupted never becomes a new transaction.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from apps.ai.service import AIError
from apps.transactions.models import Transaction, TransactionChange
from apps.whatsapp import hints, messages
from apps.whatsapp.client import EvolutionHTTPError
from apps.whatsapp.models import InboundMessage
from common.formatting import MONTHS

from .test_audio import post_audio
from .test_image import post_image
from .test_mutation_targets import create, ddmm, ref, today, undo, update
from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db
Action = TransactionChange.Action


def day_n(n: int) -> date:
    """docs/DOMAIN.md §4: "dia N" = this month if N <= today's day, else last month."""
    t = today()
    if n <= t.day:
        return t.replace(day=n)
    return (t.replace(day=1) - timedelta(days=1)).replace(day=n)


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def previous_month(d: date) -> date:
    return d.replace(day=1) - timedelta(days=1)


def code_of(record) -> str:
    record.refresh_from_db()
    return record.error_code


def assert_no_mutation():
    assert not TransactionChange.objects.filter(
        action__in=[Action.UPDATE, Action.DELETE, Action.UNDO]
    ).exists()
    assert not Transaction.objects.deleted().exists()


@pytest.fixture
def two_incomes(alice, catalog, make_transaction):
    """Incomes on "dia 5" and "dia 7" (relative to today, per the project's rule)."""
    t = make_transaction
    return {
        5: t(
            user=alice,
            type="INCOME",
            category=catalog["outras_receitas"],
            amount="50",
            transaction_date=day_n(5),
        ),
        7: t(
            user=alice,
            type="INCOME",
            category=catalog["outras_receitas"],
            amount="70",
            transaction_date=day_n(7),
        ),
    }


@pytest.fixture
def months(alice, catalog, make_transaction):
    """One income this month, one in the previous month."""
    t = make_transaction
    this = today()
    last = previous_month(this)
    return {
        "this": t(
            user=alice,
            type="INCOME",
            category=catalog["outras_receitas"],
            amount="50",
            transaction_date=this,
        ),
        "last": t(
            user=alice,
            type="INCOME",
            category=catalog["outras_receitas"],
            amount="70",
            transaction_date=last,
        ),
    }


# --- P1#1: textual dates are values, not flags ------------------------------------------------


def test_unit_temporal_spans_are_values():
    t = date(2026, 10, 16)  # Friday
    assert hints.temporal("apague a entrada do dia 5", t).spans == ((date(2026, 10, 5),) * 2,)
    assert hints.temporal("dia 20", t).spans == ((date(2026, 9, 20),) * 2,)
    assert hints.temporal("07/10", t).spans == ((date(2026, 10, 7),) * 2,)
    assert hints.temporal("20/10", t).spans == ((date(2025, 10, 20),) * 2,)
    assert hints.temporal("07/10/2025", t).spans == ((date(2025, 10, 7),) * 2,)
    assert hints.temporal("outubro", t).spans == ((date(2026, 10, 1), date(2026, 10, 31)),)
    assert hints.temporal("novembro", t).spans == ((date(2025, 11, 1), date(2025, 11, 30)),)
    assert hints.temporal("outubro de 2025", t).spans == ((date(2025, 10, 1), date(2025, 10, 31)),)
    assert hints.temporal("5 de outubro", t).spans == ((date(2026, 10, 5),) * 2,)
    assert hints.temporal("anteontem", t).spans == ((date(2026, 10, 14),) * 2,)
    assert hints.temporal("o gasto de quarta", t).spans == ((date(2026, 10, 14),) * 2,)
    assert hints.temporal("mês passado", t).spans == ((date(2026, 9, 1), date(2026, 9, 30)),)
    assert not hints.temporal("o gasto da feira", t).spans


@pytest.mark.parametrize(
    "text",
    ["dia 45", "31/02", "sexta passada", "a segunda entrada", "todo mês", "próxima sexta"],
)
def test_unit_unresolvable_dates(text):
    assert hints.temporal(text, date(2026, 10, 16)).unresolved


def test_delete_day_n_model_points_to_other_day(client, alice, two_incomes, ai, evolution):
    ai.set(undo(ref(transaction_type="INCOME", transaction_date=day_n(7).isoformat())))
    record = deliver(client, "apague a entrada do dia 5")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


def test_update_day_n_model_points_to_other_day(client, alice, two_incomes, ai, evolution):
    ai.set(
        update(ref(transaction_type="INCOME", transaction_date=day_n(7).isoformat()), amount="40")
    )
    record = deliver(client, "altere a entrada do dia 5 para 40")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


def test_delete_day_n_correct_proposal_works(client, alice, two_incomes, ai, evolution):
    ai.set(undo(ref(transaction_type="INCOME", transaction_date=day_n(5).isoformat())))
    record = deliver(client, "apague a entrada do dia 5")
    assert code_of(record) == ""
    assert set(Transaction.objects.deleted()) == {two_incomes[5]}


def test_update_day_n_correct_proposal_works(client, alice, two_incomes, ai, evolution):
    ai.set(
        update(ref(transaction_type="INCOME", transaction_date=day_n(5).isoformat()), amount="40")
    )
    record = deliver(client, "altere a entrada do dia 5 para 40")
    assert code_of(record) == ""
    two_incomes[5].refresh_from_db()
    two_incomes[7].refresh_from_db()
    assert (two_incomes[5].amount, two_incomes[7].amount) == (Decimal("40.00"), Decimal("70.00"))


def test_delete_month_model_points_to_previous_month(client, alice, months, ai, evolution):
    last = months["last"].transaction_date
    ai.set(undo(ref(transaction_type="INCOME", transaction_date=last.isoformat())))
    record = deliver(client, f"apague a entrada de {MONTHS[today().month - 1]}")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


def test_bulk_delete_month_model_points_to_previous_month(client, alice, months, ai, evolution):
    last = months["last"].transaction_date
    ai.set(
        undo(
            ref(
                all=True,
                transaction_type="INCOME",
                start_date=month_start(last).isoformat(),
                end_date=month_end(last).isoformat(),
            )
        )
    )
    record = deliver(client, f"apague as entradas de {MONTHS[today().month - 1]}")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert not InboundMessage.objects.filter(error_code="CONFIRMATION_REQUIRED").exists()
    assert_no_mutation()


def test_bulk_delete_month_correct_period_asks_confirmation(client, alice, months, ai, evolution):
    this = today()
    ai.set(
        undo(
            ref(
                all=True,
                transaction_type="INCOME",
                start_date=month_start(this).isoformat(),
                end_date=this.isoformat(),
            )
        )
    )
    record = deliver(client, f"apague as entradas de {MONTHS[this.month - 1]}")
    assert code_of(record) == "CONFIRMATION_REQUIRED"
    assert_no_mutation()


def test_update_month_and_year_model_points_to_other_month(client, alice, months, ai, evolution):
    this, last = today(), months["last"].transaction_date
    ai.set(
        update(
            ref(
                transaction_type="INCOME",
                start_date=month_start(last).isoformat(),
                end_date=month_end(last).isoformat(),
            ),
            amount="40",
        )
    )
    record = deliver(client, f"altere a entrada de {MONTHS[this.month - 1]} de {this.year} para 40")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


def test_update_month_and_year_correct_proposal_works(client, alice, months, ai, evolution):
    this = today()
    ai.set(
        update(
            ref(
                transaction_type="INCOME",
                start_date=month_start(this).isoformat(),
                end_date=month_end(this).isoformat(),
            ),
            amount="40",
        )
    )
    record = deliver(client, f"altere a entrada de {MONTHS[this.month - 1]} de {this.year} para 40")
    assert code_of(record) == ""
    months["this"].refresh_from_db()
    assert months["this"].amount == Decimal("40.00")


@pytest.mark.parametrize("verb", ["apague", "altere"])
def test_ddmm_divergent(client, alice, two_incomes, ai, evolution, verb):
    proposal = ref(transaction_type="INCOME", transaction_date=day_n(7).isoformat())
    ai.set(undo(proposal) if verb == "apague" else update(proposal, amount="40"))
    suffix = " para 40" if verb == "altere" else ""
    record = deliver(client, f"{verb} a entrada de {ddmm(day_n(5))}{suffix}")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


@pytest.mark.parametrize(
    "word, said_offset, model_offset",
    [("hoje", 0, 1), ("ontem", 1, 0), ("ontem", 1, 2), ("anteontem", 2, 1)],
)
@pytest.mark.parametrize("verb", ["apague", "altere"])
def test_relative_day_divergent(
    client, alice, catalog, make_transaction, ai, evolution, word, said_offset, model_offset, verb
):
    for offset in (0, 1, 2):
        make_transaction(
            user=alice,
            type="INCOME",
            category=catalog["outras_receitas"],
            amount=str(50 + offset),
            transaction_date=today() - timedelta(days=offset),
        )
    model_day = (today() - timedelta(days=model_offset)).isoformat()
    proposal = ref(transaction_type="INCOME", transaction_date=model_day)
    ai.set(undo(proposal) if verb == "apague" else update(proposal, amount="40"))
    suffix = " para 40" if verb == "altere" else ""
    record = deliver(client, f"{verb} a entrada de {word}{suffix}")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


@pytest.mark.parametrize(
    "text",
    [
        "apague a entrada da sexta passada",
        "apague a entrada do dia 45",
        "apague a entrada de 31/02",
        "apague a entrada do mês retrasado",
    ],
)
def test_unresolvable_date_mutates_nothing(client, alice, two_incomes, ai, evolution, text):
    ai.set(undo(ref(transaction_type="INCOME", transaction_date=day_n(5).isoformat())))
    record = deliver(client, text)
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


def test_unresolvable_date_in_update_mutates_nothing(client, alice, two_incomes, ai, evolution):
    ai.set(
        update(ref(transaction_type="INCOME", transaction_date=day_n(5).isoformat()), amount="40")
    )
    record = deliver(client, "altere a entrada da sexta passada para 40")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


def test_identifying_date_used_only_as_new_date_is_refused(
    client, alice, two_incomes, ai, evolution
):
    """ "a entrada do dia 5": the day names the target; as only the new date → ask."""
    ai.set(update(ref(transaction_type="INCOME"), transaction_date=day_n(5).isoformat()))
    record = deliver(client, "altere a entrada do dia 5")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


# --- P1#2: UPDATE never selects by criteria the model invented ---------------------------------


@pytest.fixture
def one_income(alice, catalog, make_transaction):
    return make_transaction(
        user=alice,
        type="INCOME",
        category=catalog["outras_receitas"],
        amount="50",
        description="freela",
        transaction_date=today() - timedelta(days=3),
    )


def _rename(**reference):
    return update(ref(transaction_type="INCOME", **reference), description="bônus")


@pytest.mark.parametrize(
    "invented",
    [
        {"amount": "50"},
        {"description": "freela"},
        {"transaction_date": "__D__"},
        {"start_date": "__D__", "end_date": "__D__"},
        {"category_id": "__CAT__"},
    ],
    ids=["amount", "old-description", "date", "period", "category"],
)
def test_rename_income_with_invented_criterion_mutates_nothing(
    client, alice, catalog, one_income, ai, evolution, invented
):
    values = {
        "__D__": one_income.transaction_date.isoformat(),
        "__CAT__": catalog["outras_receitas"].pk,
    }
    reference = {name: values.get(value, value) for name, value in invented.items()}
    ai.set(_rename(**reference))
    record = deliver(client, "altere a descrição da entrada para bônus")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()
    one_income.refresh_from_db()
    assert one_income.description == "freela"


def test_rename_expense_with_invented_subcategory_mutates_nothing(
    client, alice, catalog, make_transaction, ai, evolution
):
    txn = make_transaction(
        user=alice,
        category=catalog["transporte"],
        subcategory=catalog["combustivel"],
        amount="30",
        transaction_date=today(),
    )
    ai.set(
        update(
            ref(transaction_type="EXPENSE", subcategory_id=catalog["combustivel"].pk),
            description="lanche",
        )
    )
    record = deliver(client, "altere a descrição do gasto para lanche")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()
    txn.refresh_from_db()
    assert txn.description == ""


def test_new_description_does_not_prove_the_target(
    client, alice, catalog, make_transaction, ai, evolution
):
    """The model reuses the new value ("bônus") as the old description: not a criterion."""
    make_transaction(
        user=alice,
        type="INCOME",
        category=catalog["outras_receitas"],
        amount="50",
        description="bônus antigo",
        transaction_date=today(),
    )
    ai.set(_rename(description="bônus"))
    record = deliver(client, "altere a descrição da entrada para bônus")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


def test_new_amount_does_not_prove_the_target(client, alice, one_income, ai, evolution):
    """ "altere a entrada para 50": 50 is the new value, not "the income of 50"."""
    ai.set(update(ref(transaction_type="INCOME", amount="50"), amount="50", description="x"))
    record = deliver(client, "altere a entrada para 50")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


def test_type_only_with_single_candidate_mutates_nothing(client, alice, one_income, ai, evolution):
    ai.set(_rename())
    record = deliver(client, "altere a descrição da entrada para bônus")
    assert code_of(record) == "CORRECTION_TARGET_UNCLEAR"
    assert_no_mutation()


def test_type_and_textual_amount_works(client, alice, one_income, ai, evolution):
    ai.set(update(ref(transaction_type="INCOME", amount="50"), amount="40"))
    record = deliver(client, "altere a entrada de 50 para 40")
    assert code_of(record) == ""
    one_income.refresh_from_db()
    assert one_income.amount == Decimal("40.00")


def test_type_and_textual_date_works(client, alice, catalog, make_transaction, ai, evolution):
    yesterday = today() - timedelta(days=1)
    txn = make_transaction(
        user=alice,
        type="INCOME",
        category=catalog["outras_receitas"],
        amount="30",
        transaction_date=yesterday,
    )
    ai.set(
        update(ref(transaction_type="INCOME", transaction_date=yesterday.isoformat()), amount="50")
    )
    record = deliver(client, "altere a entrada de ontem para 50")
    assert code_of(record) == ""
    txn.refresh_from_db()
    assert txn.amount == Decimal("50.00")


def test_explicit_last_works(client, alice, one_income, ai, evolution):
    ai.set(update(ref(transaction_type="INCOME", last=True), amount="60"))
    record = deliver(client, "altere a última entrada para 60")
    assert code_of(record) == ""
    one_income.refresh_from_db()
    assert one_income.amount == Decimal("60.00")


def test_explicit_last_with_invented_filter_mutates_nothing(
    client, alice, catalog, one_income, make_transaction, ai, evolution
):
    """ "a última entrada" is the latest income; an invented amount would pick another one."""
    make_transaction(
        user=alice,
        type="INCOME",
        category=catalog["salario"],
        amount="900",
        transaction_date=today(),
    )
    ai.set(update(ref(transaction_type="INCOME", last=True, amount="50"), description="bônus"))
    record = deliver(client, "altere a descrição da última entrada para bônus")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_no_mutation()


def test_contextual_correction_still_works(client, alice, catalog, ai, evolution):
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    ai.set(update(correction=True, amount="80"))
    record = deliver(client, "errado, foram 80", "M2")
    assert code_of(record) == ""
    txn = Transaction.objects.get()
    assert txn.amount == Decimal("80.00")


# --- P1#4: an interrupted correction context never turns "80" into a CREATE -------------------


def _registered_100(client, ai, catalog):
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    assert Transaction.objects.count() == 1


def _elliptic(client, ai, catalog, text="80"):
    ai.set(create("EXPENSE", "80", catalog["outros_gastos"]))
    record = deliver(client, text, "M3")
    assert code_of(record) == "CORRECTION_NOT_CREATED"
    assert Transaction.objects.count() == 1
    assert Transaction.objects.get().amount == Decimal("100.00")
    return record


@pytest.mark.parametrize(
    "between_text, between",
    [
        ("ajuda", {"intent": "HELP"}),
        ("asdfgh", {"intent": "UNKNOWN"}),
        ("beleza", {"intent": "UNKNOWN"}),
        ("quanto gastei esse mês?", {"intent": "QUERY_TRANSACTIONS", "query_kind": "TOTAL"}),
    ],
    ids=["help", "unknown", "no-effect", "query"],
)
@pytest.mark.parametrize("elliptic", ["80", "ok, 80"])
def test_interrupted_context_elliptic_amount_creates_nothing(
    client, alice, catalog, ai, evolution, between_text, between, elliptic
):
    _registered_100(client, ai, catalog)
    if between["intent"] == "QUERY_TRANSACTIONS":
        between = {**between, "transaction_type": "EXPENSE"}
    ai.set(interp(**between))
    deliver(client, between_text, "M2")
    _elliptic(client, ai, catalog, elliptic)
    assert messages.CORRECTION_TARGET_UNCLEAR in evolution.call_args.args[1]


def test_ai_failure_then_elliptic_amount_creates_nothing(client, alice, catalog, ai, evolution):
    _registered_100(client, ai, catalog)
    ai.side_effect = AIError("TIMEOUT")
    record = deliver(client, "manda o resumo", "M2")
    assert code_of(record) == "AI_TIMEOUT"
    ai.side_effect = None
    _elliptic(client, ai, catalog)


def test_invalid_audio_then_elliptic_amount_creates_nothing(client, alice, catalog, ai, evolution):
    _registered_100(client, ai, catalog)
    with mock.patch("apps.whatsapp.audio.EvolutionClient") as cls:
        cls.return_value.download_audio.side_effect = EvolutionHTTPError(400, "gone")
        record = post_audio(client, message_id="M2")
    assert code_of(record) == "AUDIO_DOWNLOAD_FAILED"
    _elliptic(client, ai, catalog)


def test_invalid_image_then_elliptic_amount_creates_nothing(client, alice, catalog, ai, evolution):
    _registered_100(client, ai, catalog)
    with mock.patch("apps.whatsapp.image.EvolutionClient") as cls:
        cls.return_value.download_image.side_effect = EvolutionHTTPError(400, "gone")
        record = post_image(client, message_id="M2")
    assert code_of(record) == "IMAGE_DOWNLOAD_FAILED"
    _elliptic(client, ai, catalog)


def test_immediate_context_elliptic_amount_still_never_creates(
    client, alice, catalog, ai, evolution
):
    _registered_100(client, ai, catalog)
    ai.set(create("EXPENSE", "80", catalog["outros_gastos"]))
    record = deliver(client, "80", "M2")
    assert code_of(record) == "CORRECTION_NOT_CREATED"
    assert Transaction.objects.count() == 1


def test_without_recent_mutation_elliptic_amount_follows_normal_policy(
    client, alice, catalog, ai, evolution
):
    ai.set(create("EXPENSE", "80", catalog["outros_gastos"]))
    record = deliver(client, "80", "M1")
    assert code_of(record) == ""
    assert Transaction.objects.get().amount == Decimal("80.00")


def test_expired_mutation_does_not_block_elliptic_amount(client, alice, catalog, ai, evolution):
    """TTL stays the upper bound: an old registration is not a correction context anymore."""
    _registered_100(client, ai, catalog)
    InboundMessage.objects.filter(provider_message_id="M1").update(
        processed_at=timezone.now() - timedelta(days=1)
    )
    ai.set(interp(intent="HELP"))
    deliver(client, "ajuda", "M2")
    ai.set(create("EXPENSE", "80", catalog["outros_gastos"]))
    record = deliver(client, "80", "M3")
    assert code_of(record) == ""
    assert Transaction.objects.count() == 2


def test_explicit_create_after_recent_mutation_still_works(client, alice, catalog, ai, evolution):
    _registered_100(client, ai, catalog)
    ai.set(interp(intent="HELP"))
    deliver(client, "ajuda", "M2")
    ai.set(create("INCOME", "80", catalog["salario"]))
    record = deliver(client, "Recebi 80 hoje", "M3")
    assert code_of(record) == ""
    assert Transaction.objects.count() == 2
