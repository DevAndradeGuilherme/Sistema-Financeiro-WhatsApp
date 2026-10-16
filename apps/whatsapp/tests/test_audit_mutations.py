"""Regression for the independent audit (REJECTED) of the UPDATE/DELETE/context patch.

Each test names the audit finding it covers (P1#1..P1#5, P2#1, P2#2) and the number of the
mandatory case in the request. Rules under test (docs/DOMAIN.md §5):
- a mutation of "the last" needs the TEXT to say so; a bare "desfazer" changes nothing, whatever
  the model returns;
- every explicit criterion is combined with AND; a criterion the text states but the model
  dropped or contradicted → asks, zero mutation;
- a family member named in the text blocks any mutation, whatever the model returns;
- a pending confirmation is answered only by the very next message, against a fingerprint of
  the presented set;
- a contextual correction only reaches the effect of the immediately previous message, and a
  corrective/elliptic message never becomes a creation.
Concurrency cases (22, 23, 43) live in ``test_mutation_concurrency.py``.
"""

from decimal import Decimal
from unittest import mock

import pytest

from apps.ai.contract import TransactionUpdates
from apps.ai.service import AIError
from apps.families.services import add_family_member, create_family
from apps.transactions.models import Transaction, TransactionChange
from apps.transactions.services import (
    soft_delete_transaction,
    update_transaction,
)
from apps.whatsapp import messages
from apps.whatsapp.client import DownloadedMedia
from apps.whatsapp.models import InboundMessage, PendingAction

from . import test_mutation_targets as targets
from .test_audio import post_audio
from .test_image import post_image
from .test_mutation_targets import (
    D6,
    D7,
    ask_all_incomes,
    assert_nothing_changed,
    assert_only_deleted,
    create,
    ddmm,
    ref,
    reply,
    state,
    today,
    undo,
    update,
)
from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db
ledger = targets.ledger  # shared fixtures
mixed = targets.mixed
Action = TransactionChange.Action
MARIA_JID = "5521988887777@s.whatsapp.net"


def update_as(reference, scope=None, **updates):
    """UPDATE proposal with a ``scope`` hint (the shared ``update`` helper has none)."""
    return interp(
        intent="UPDATE_TRANSACTION",
        reference=reference,
        updates=TransactionUpdates(**updates),
        scope=scope,
    )


def code_of(record):
    record.refresh_from_db()
    return record.error_code


@pytest.fixture
def audio_download():
    with mock.patch("apps.whatsapp.audio.EvolutionClient") as cls:
        yield cls.return_value.download_audio


@pytest.fixture
def transcribe():
    with mock.patch("apps.whatsapp.audio.transcribe_audio") as fn:
        yield fn


@pytest.fixture
def image_download():
    with mock.patch("apps.whatsapp.image.EvolutionClient") as cls:
        yield cls.return_value.download_image


@pytest.fixture
def vision():
    with mock.patch("apps.whatsapp.image.extract_receipt") as fn:
        yield fn


# --- P1#1: no "last" without the text saying so -----------------------------------------------


@pytest.fixture
def just_created(client, alice, catalog, ai, evolution):
    """A transaction created through the channel: the undo stack is not empty."""
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M0")
    return Transaction.objects.get()


@pytest.mark.parametrize("text", ["desfazer", "remove", "apaga", "cancela", "Desfaz!"])
def test_bare_undo_words_change_nothing(client, just_created, ai, evolution, text):
    """Cases 1-4: a bare verb identifies nothing → zero mutation (never the undo stack)."""
    before = state()
    ai.set(undo())
    record = deliver(client, text, "M1")

    assert_nothing_changed(before)
    # "cancela" alone is a confirmation answer (never sent to the model): nothing pending.
    assert code_of(record) in ("TARGET_UNCLEAR", "NO_PENDING_ACTION")
    if record.error_code == "TARGET_UNCLEAR":
        assert reply(evolution) == messages.REMOVE_TARGET_UNCLEAR


@pytest.mark.parametrize(
    "proposal",
    [
        undo(ref(last=True)),
        undo(ref(last=True, transaction_type="EXPENSE")),
        update(ref(last=True), amount="1"),
    ],
    ids=["undo-last", "undo-last-expense", "update-last"],
)
@pytest.mark.parametrize("text", ["desfazer", "remove", "apaga"])
def test_model_last_without_marker_in_text_is_ignored(
    client, just_created, ai, evolution, proposal, text
):
    """Case 5: the model's ``last=true`` (even with a type) never selects "the latest", nor
    turns a bare verb into a context correction."""
    before = state()
    ai.set(proposal)
    record = deliver(client, text, "M1")
    assert_nothing_changed(before)
    assert code_of(record) in ("TARGET_UNCLEAR", "CORRECTION_TARGET_UNCLEAR")


@pytest.mark.parametrize("proposal", [undo(ref(last=True)), undo()], ids=["model", "model-lost"])
def test_undo_last_transaction_with_marker(client, alice, mixed, ai, evolution, proposal):
    """Case 6: "desfaça o último lançamento" → the most recently registered, either type."""
    ai.set(proposal)
    deliver(client, "desfaça o último lançamento")
    assert_only_deleted(mixed["Z"])


@pytest.mark.parametrize("proposal", [undo(ref(last=True)), undo()], ids=["model", "model-lost"])
def test_last_expense_is_only_expense(client, alice, mixed, ai, evolution, proposal):
    """Case 7: "último gasto" = EXPENSE even if the model drops the type."""
    ai.set(proposal)
    deliver(client, "remove o último gasto")
    assert_only_deleted(mixed["Z"])
    assert Transaction.objects.deleted().get().type == "EXPENSE"


@pytest.mark.parametrize("proposal", [undo(ref(last=True)), undo()], ids=["model", "model-lost"])
def test_last_income_is_only_income(client, alice, mixed, ai, evolution, proposal):
    """Case 8: "última entrada" = INCOME even if the model drops the type."""
    ai.set(proposal)
    deliver(client, "apague a última entrada")
    assert_only_deleted(mixed["Y"])


def test_cancel_what_i_just_registered(client, just_created, ai, evolution):
    ai.set(undo(ref(last=True)))
    deliver(client, "cancela o que acabei de registrar", "M1")
    just_created.refresh_from_db()
    assert just_created.deleted_at is not None


# --- P1#2: every criterion, AND, nothing dropped ---------------------------------------------


@pytest.fixture
def lunches(alice, catalog, make_transaction):
    t = make_transaction
    return {
        "almoco": t(
            user=alice,
            category=catalog["alimentacao"],
            amount="50",
            description="almoço",
            transaction_date=D7,
        ),
        "jantar": t(
            user=alice,
            category=catalog["alimentacao"],
            amount="50",
            description="jantar",
            transaction_date=D7,
        ),
    }


ALMOCO = dict(description="almoço", amount="50")


def test_category_description_and_amount_are_combined(
    client, alice, catalog, lunches, ai, evolution
):
    """Case 9: Alimentação AND "almoço" AND 50 → only the lunch, never the dinner."""
    ai.set(undo(ref(category_id=catalog["alimentacao"].pk, **ALMOCO)))
    deliver(client, "remova Alimentação, descrição almoço, de 50")
    assert_only_deleted(lunches["almoco"])


def test_category_matches_but_description_does_not(client, alice, catalog, lunches, ai, evolution):
    """Case 10: only the dinner exists → NOT_FOUND; a broader search is never tried."""
    lunches["almoco"].mark_deleted()
    lunches["almoco"].save()
    before = state()
    ai.set(undo(ref(category_id=catalog["alimentacao"].pk, **ALMOCO)))
    record = deliver(client, "remova Alimentação, descrição almoço, de 50")
    assert_nothing_changed(before)
    assert code_of(record) == "NOT_FOUND"
    assert reply(evolution) == (
        "Não encontrei um lançamento de R$ 50,00 em Alimentação com “almoço”."
    )


@pytest.mark.parametrize(
    "proposal",
    [
        # Case 11: the amount stated as a criterion (50) was dropped.
        update(ref(transaction_type="INCOME", transaction_date=D7.isoformat()), amount="40"),
        # The amount was contradicted (60 is not in the text).
        update(
            ref(transaction_type="INCOME", amount="60", transaction_date=D7.isoformat()),
            amount="40",
        ),
        # Case 12: the date stated as a criterion was dropped.
        update(ref(transaction_type="INCOME", amount="50"), amount="40"),
        # The date was contradicted (06/10 instead of 07/10).
        update(
            ref(transaction_type="INCOME", amount="50", transaction_date=D6.isoformat()),
            amount="40",
        ),
        # Case 13: the type contradicts the text ("entrada").
        update(
            ref(transaction_type="EXPENSE", amount="50", transaction_date=D7.isoformat()),
            amount="40",
        ),
    ],
    ids=["amount-dropped", "amount-contradicted", "date-dropped", "date-contradicted", "type"],
)
def test_update_with_dropped_or_contradicted_criterion_changes_nothing(
    client, alice, ledger, ai, evolution, proposal
):
    before = state()
    ai.set(proposal)
    record = deliver(client, f"a entrada de 50 do dia {ddmm(D7)} era 40")
    assert_nothing_changed(before)
    assert code_of(record) == "TARGET_UNCLEAR"
    assert reply(evolution) == messages.CORRECTION_TARGET_UNCLEAR


def test_update_with_every_criterion_works(client, alice, ledger, ai, evolution):
    ai.set(
        update(
            ref(transaction_type="INCOME", amount="50", transaction_date=D7.isoformat()),
            amount="40",
        )
    )
    deliver(client, f"a entrada de 50 do dia {ddmm(D7)} era 40")
    ledger["income_50"].refresh_from_db()
    assert ledger["income_50"].amount == Decimal("40.00")


@pytest.mark.parametrize(
    "proposal, text",
    [
        # Case 14: the category named in the text was dropped (there is one gasto of 12)...
        (
            lambda c: undo(ref(transaction_type="EXPENSE", amount="12")),
            "remova o gasto de transporte de 12",
        ),
        # ...or contradicted.
        (
            lambda c: undo(
                ref(transaction_type="EXPENSE", amount="12", category_id=c["alimentacao"].pk)
            ),
            "remova o gasto de transporte de 12",
        ),
        # A subcategory/alias ("gasolina" = Combustível) dropped.
        (lambda c: undo(ref(amount="12")), "apague a gasolina de 12"),
        # UPDATE: the item is neither a criterion nor the new value.
        (
            lambda c: update(ref(transaction_type="EXPENSE", amount="12"), amount="15"),
            "o uber de 12 era 15",
        ),
    ],
    ids=["category-dropped", "category-contradicted", "alias-dropped", "update-item-dropped"],
)
def test_catalog_item_dropped_or_contradicted_changes_nothing(
    client, alice, catalog, ledger, ai, evolution, proposal, text
):
    before = state()
    ai.set(proposal(catalog))
    record = deliver(client, text)
    assert_nothing_changed(before)
    assert code_of(record) == "TARGET_UNCLEAR"


def test_catalog_item_as_new_value_is_accepted(client, alice, catalog, ledger, ai, evolution):
    """The category named in an UPDATE may be the *new* value ("muda ... para Alimentação")."""
    ai.set(
        update(
            ref(transaction_type="EXPENSE", amount="12"),
            category_id=catalog["alimentacao"].pk,
        )
    )
    deliver(client, "muda o gasto de 12 para alimentação")
    target = ledger["expenses"][0]
    target.refresh_from_db()
    assert target.category == catalog["alimentacao"]


def test_model_invented_criteria_are_refused(client, alice, ledger, ai, evolution):
    """The proposal may not add an amount or a date the text does not show."""
    before = state()
    ai.set(undo(ref(transaction_type="INCOME", transaction_date=D7.isoformat())))
    record = deliver(client, "Desfaça a entrada", "M1")
    assert code_of(record) == "TARGET_UNCLEAR"
    ai.set(undo(ref(transaction_type="INCOME", amount="50")))
    record = deliver(client, "Desfaça a entrada de 1050,41", "M2")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert_nothing_changed(before)


# --- P1#3: a family member named in the text blocks every mutation ----------------------------


@pytest.fixture
def twins(alice, maria, catalog, make_transaction):
    """Alice (ADMIN) and Maria (MEMBER) each have an INCOME of 77 on the same day."""
    family = create_family(name="Família", admin=alice)
    add_family_member(family=family, user=maria)
    t = make_transaction
    return {
        "alice": t(
            user=alice, type="INCOME", category=catalog["salario"], amount="77", transaction_date=D7
        ),
        "maria": t(
            user=maria, type="INCOME", category=catalog["salario"], amount="77", transaction_date=D7
        ),
    }


MODEL_OMITS_MARIA = [
    undo(ref(transaction_type="INCOME", amount="77")),
    undo(ref(transaction_type="INCOME", amount="77"), scope="SELF"),
    undo(ref(transaction_type="INCOME", amount="77", transaction_date=D7.isoformat())),
    undo(ref(all=True, transaction_type="INCOME", amount="77")),
]


@pytest.mark.parametrize("proposal", MODEL_OMITS_MARIA)
def test_admin_naming_member_deletes_nothing(client, twins, ai, evolution, proposal):
    """Case 15: neither Maria's nor Alice's look-alike income is touched."""
    before = state()
    ai.set(proposal)
    record = deliver(client, "apague a entrada de 77 da Maria", "M1")
    deliver(client, "sim", "M2")
    assert_nothing_changed(before)
    assert code_of(record) == "FORBIDDEN_OTHER_ACCOUNT"
    assert not PendingAction.objects.exists()


@pytest.mark.parametrize(
    "proposal",
    [
        update_as(ref(transaction_type="INCOME", amount="77"), amount="1"),
        update_as(ref(transaction_type="INCOME", amount="77"), scope="SELF", amount="1"),
        update_as(ref(transaction_type="INCOME", amount="77", last=True), amount="1"),
    ],
)
def test_admin_naming_member_updates_nothing(client, twins, ai, evolution, proposal):
    """Case 16: same for UPDATE."""
    before = state()
    ai.set(proposal)
    record = deliver(client, "a entrada de 77 da Maria era 1", "M1")
    assert_nothing_changed(before)
    assert code_of(record) == "FORBIDDEN_OTHER_ACCOUNT"
    assert reply(evolution) == messages.OWN_CHANGES_ONLY


def test_member_naming_admin_by_username_changes_nothing(client, twins, ai, evolution):
    """Alice has no first name: she is known by her username (as in replies)."""
    before = state()
    ai.set(undo(ref(transaction_type="INCOME", amount="77")))
    record = deliver(client, "apaga a entrada de 77 da alice", "M1", remote_jid=MARIA_JID)
    assert_nothing_changed(before)
    assert code_of(record) == "FORBIDDEN_OTHER_ACCOUNT"


def test_own_mutation_without_names_still_works(client, twins, ai, evolution):
    ai.set(undo(ref(transaction_type="INCOME", amount="77")))
    deliver(client, "apague a entrada de 77", "M1")
    assert_only_deleted(twins["alice"])


def test_family_queries_are_unchanged(client, twins, ai, evolution):
    ai.set(
        interp(
            intent="QUERY_TRANSACTIONS",
            query_kind="TOTAL",
            transaction_type="INCOME",
            scope="FAMILY_MEMBER",
            target_member_name="Maria",
            start_date=D7.isoformat(),
            end_date=today().isoformat(),
        )
    )
    record = deliver(client, "quanto a Maria recebeu?", "Q1")
    assert code_of(record) == ""
    assert "R$ 77,00" in reply(evolution)


# --- PendingAction snapshot (17-21) -----------------------------------------------------------


def _recategorize(alice, ledger, catalog):
    update_transaction(
        user=alice,
        transaction_id=ledger["income_1050"].pk,
        category=catalog["outras_receitas"],
    )


def _redescribe(alice, ledger, catalog):
    update_transaction(user=alice, transaction_id=ledger["income_50"].pk, description="bônus")


def _redate(alice, ledger, catalog):
    update_transaction(user=alice, transaction_id=ledger["income_50"].pk, transaction_date=D6)


def _swap_amounts(alice, ledger, catalog):
    """1050.41 ↔ 50: same count, same total, different pairing id ↔ amount."""
    update_transaction(user=alice, transaction_id=ledger["income_50"].pk, amount="1050.41")
    update_transaction(user=alice, transaction_id=ledger["income_1050"].pk, amount="50")


def _soft_delete_one(alice, ledger, catalog):
    soft_delete_transaction(actor=alice, transaction_id=ledger["income_50"].pk)


@pytest.mark.parametrize(
    "change",
    [_recategorize, _redescribe, _redate, _swap_amounts, _soft_delete_one],
    ids=["17-category", "18-description", "19-date", "20-swap-keeps-total", "21-deleted"],
)
def test_confirmation_refused_when_a_candidate_changed(
    client, alice, catalog, ledger, ai, evolution, change
):
    ask_all_incomes(client, ai)
    change(alice, ledger, catalog)
    deletes_before = TransactionChange.objects.filter(action=Action.DELETE).count()
    record = deliver(client, "sim", "C1")
    assert code_of(record) == "CONFIRMATION_STALE"
    assert reply(evolution) == messages.CONFIRMATION_STALE
    assert TransactionChange.objects.filter(action=Action.DELETE).count() == deletes_before
    assert not PendingAction.objects.exists()


def test_pending_action_stores_snapshot_and_asking_message(client, alice, ledger, ai, evolution):
    record = ask_all_incomes(client, ai)
    pending = PendingAction.objects.get()
    assert pending.inbound_message == record
    assert len(pending.fingerprint) == 64
    assert pending.criteria == {"type": "INCOME"}


# --- P1#5: any message in between invalidates the confirmation (24-30) ------------------------


def _another_request(client, ai, **_):
    ai.set(undo(ref(transaction_type="INCOME", amount="999")))  # no effect (NOT_FOUND)
    deliver(client, "apague a entrada de 999", "X1")


def _openai_failure(client, ai, **_):
    ai.side_effect = AIError("TIMEOUT")
    deliver(client, "quanto gastei?", "X1")
    ai.side_effect = None


def _help(client, ai, **_):
    ai.set(interp(intent="HELP"))
    deliver(client, "ajuda", "X1")


def _unknown(client, ai, **_):
    ai.set(interp(intent="UNKNOWN"))
    deliver(client, "bom dia", "X1")


def _invalid_audio(client, ai, audio_download, **_):
    audio_download.return_value = DownloadedMedia(b"not audio at all", "audio/ogg")
    post_audio(client, message_id="X1")


def _invalid_image(client, ai, image_download, vision, **_):
    image_download.return_value = DownloadedMedia(b"not an image", "image/jpeg")
    post_image(client, message_id="X1")
    vision.assert_not_called()


INTERRUPTIONS = [
    _another_request,
    _openai_failure,
    _help,
    _unknown,
    _invalid_audio,
    _invalid_image,
]
INTERRUPTION_IDS = [
    "24-other-request",
    "25-openai-failure",
    "26-help",
    "27-unknown",
    "28-invalid-audio",
    "29-invalid-image",
]


@pytest.mark.parametrize("interrupt", INTERRUPTIONS, ids=INTERRUPTION_IDS)
def test_yes_after_any_other_message_deletes_nothing(
    client, alice, ledger, ai, evolution, audio_download, image_download, vision, interrupt
):
    """Case 30: "sim" only answers the question right before it."""
    ask_all_incomes(client, ai)
    interrupt(
        client, ai, audio_download=audio_download, image_download=image_download, vision=vision
    )
    deletes_before = TransactionChange.objects.filter(action=Action.DELETE).count()
    record = deliver(client, "sim", "C1")
    assert code_of(record) == "NO_PENDING_ACTION"
    assert TransactionChange.objects.filter(action=Action.DELETE).count() == deletes_before
    assert not Transaction.objects.deleted().exists()
    assert not PendingAction.objects.exists()


def test_message_ignored_after_identifying_sender_also_invalidates(
    client, alice, ledger, ai, evolution
):
    """An unsupported message from the sender (e.g. sticker) is still a new message."""
    ask_all_incomes(client, ai)
    InboundMessage.objects.create(
        instance="test-instance",
        provider_message_id="STICKER",
        message_type="UNSUPPORTED",
        user=alice,
        status=InboundMessage.Status.IGNORED,
        ignore_reason=InboundMessage.IgnoreReason.UNSUPPORTED_TYPE,
    )
    record = deliver(client, "sim", "C1")
    assert code_of(record) == "NO_PENDING_ACTION"
    assert not Transaction.objects.deleted().exists()


def test_other_users_messages_do_not_invalidate(client, alice, maria, ledger, ai, evolution):
    ask_all_incomes(client, ai)
    ai.set(interp(intent="HELP"))
    deliver(client, "ajuda", "X1", remote_jid=MARIA_JID)
    deliver(client, "sim", "C1")
    assert_only_deleted(ledger["income_50"], ledger["income_1050"])


def test_audio_yes_right_after_the_question_confirms(
    client, alice, ledger, ai, evolution, audio_download, transcribe
):
    from apps.ai.transcription import TranscriptionResult

    from .test_audio import OGG

    ask_all_incomes(client, ai)
    audio_download.return_value = DownloadedMedia(OGG, "audio/ogg")
    transcribe.return_value = TranscriptionResult(text="sim", model="t", duration_ms=1)
    post_audio(client, message_id="A1")
    assert_only_deleted(ledger["income_50"], ledger["income_1050"])


def test_redelivered_confirmation_does_not_run_again(client, alice, ledger, ai, evolution):
    """Case 42."""
    ask_all_incomes(client, ai)
    deliver(client, "sim", "C1")
    deliver(client, "sim", "C1")  # webhook retry: same provider id
    assert TransactionChange.objects.filter(action=Action.DELETE).count() == 2
    assert InboundMessage.objects.filter(provider_message_id="C1").count() == 1
    deliver(client, "sim", "C2")  # a new "sim": consumed, nothing pending
    assert TransactionChange.objects.filter(action=Action.DELETE).count() == 2


# --- P2#1: contextual correction tied to the previous message (31-35) -------------------------


def _help_between(client, ai, **_):
    _help(client, ai)


def _unknown_between(client, ai, **_):
    _unknown(client, ai)


def _openai_failure_between(client, ai, **_):
    _openai_failure(client, ai)


def _invalid_audio_between(client, ai, audio_download, **_):
    _invalid_audio(client, ai, audio_download)


def _query_between(client, ai, **_):
    ai.set(interp(intent="QUERY_TRANSACTIONS", query_kind="BALANCE"))
    deliver(client, "meu saldo", "X1")


def _no_effect_between(client, ai, **_):
    _another_request(client, ai)


@pytest.mark.parametrize(
    "between",
    [
        _help_between,
        _unknown_between,
        _openai_failure_between,
        _invalid_audio_between,
        _query_between,
        _no_effect_between,
    ],
    ids=["31-help", "32-unknown", "33-openai", "34-invalid-audio", "query", "no-effect"],
)
def test_correction_after_another_message_never_edits_the_old_transaction(
    client, alice, catalog, ai, evolution, audio_download, between
):
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    between(client, ai, audio_download=audio_download)
    ai.set(update(correction=True, amount="80"))
    record = deliver(client, "errado, foram 80", "M2")

    assert Transaction.objects.get().amount == Decimal("100.00")
    assert code_of(record) == "CORRECTION_TARGET_UNCLEAR"
    assert not TransactionChange.objects.filter(action=Action.UPDATE).exists()


def test_correction_after_another_create_reaches_only_that_one(
    client, alice, catalog, ai, evolution
):
    """Case 35: the correction goes to the effect of the message right before it."""
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    ai.set(create("EXPENSE", "50", catalog["transporte"], "uber"))
    deliver(client, "50 de uber", "M2")
    ai.set(update(correction=True, amount="80"))
    deliver(client, "errado, foram 80", "M3")

    amounts = dict(Transaction.objects.values_list("description", "amount"))
    assert amounts == {"mercado": Decimal("100.00"), "uber": Decimal("80.00")}


def test_correction_after_an_update_edits_the_same_transaction(
    client, alice, catalog, ai, evolution
):
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    ai.set(update(correction=True, amount="90"))
    deliver(client, "errado, foram 90", "M2")
    ai.set(update(correction=True, amount="80"))
    deliver(client, "não, foram 80", "M3")
    assert Transaction.objects.get().amount == Decimal("80.00")


def test_correction_after_the_transaction_was_edited_elsewhere_asks(
    client, alice, catalog, ai, evolution
):
    """The previous message's effect is no longer the latest change of that transaction."""
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    txn = Transaction.objects.get()
    update_transaction(user=alice, transaction_id=txn.pk, amount="95", origin="manual")
    ai.set(update(correction=True, amount="80"))
    record = deliver(client, "errado, foram 80", "M2")
    assert code_of(record) == "CORRECTION_TARGET_UNCLEAR"
    txn.refresh_from_db()
    assert txn.amount == Decimal("95.00")


def test_audio_correction_follows_the_same_rule(
    client, alice, catalog, ai, evolution, audio_download, transcribe
):
    from apps.ai.transcription import TranscriptionResult

    from .test_audio import OGG

    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    audio_download.return_value = DownloadedMedia(OGG, "audio/ogg")
    transcribe.return_value = TranscriptionResult(text="errado, foram 80", model="t", duration_ms=1)
    ai.set(update(correction=True, amount="80"))
    post_audio(client, message_id="A1")
    assert Transaction.objects.get().amount == Decimal("80.00")

    transcribe.return_value = TranscriptionResult(text="desfazer", model="t", duration_ms=1)
    ai.set(undo(ref(last=True)))
    record = post_audio(client, message_id="A2")
    assert code_of(record) == "TARGET_UNCLEAR"
    assert Transaction.objects.active().count() == 1


# --- P2#2: corrective phrases never become a CREATE (36-40) -----------------------------------

CORRECTIVE = ["Desculpa, foram 80", "Não, foram 80", "Foi 80, não 100"]


@pytest.mark.parametrize("text", CORRECTIVE)
@pytest.mark.parametrize("after_create", [True, False], ids=["after-create", "cold"])
def test_corrective_phrases_never_create(client, alice, catalog, ai, evolution, text, after_create):
    """Cases 36-38: even if the model proposes CREATE without ``correction``."""
    if after_create:
        ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
        deliver(client, "Gastei 100 no mercado", "M1")
    count = Transaction.objects.count()
    ai.set(create("EXPENSE", "80", catalog["outros_gastos"]))
    record = deliver(client, text, "M2")
    assert Transaction.objects.count() == count
    assert code_of(record) == "CORRECTION_NOT_CREATED"


@pytest.mark.parametrize("text", CORRECTIVE)
def test_corrective_phrases_as_update_edit_the_previous_effect(
    client, alice, catalog, ai, evolution, text
):
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    ai.set(update(correction=True, amount="80"))
    deliver(client, text, "M2")
    assert Transaction.objects.get().amount == Decimal("80.00")


@pytest.mark.parametrize("text", ["80", "80 reais", "R$ 80,00", "ok, 80"])
def test_elliptic_amount_right_after_a_mutation_is_not_created(
    client, alice, catalog, ai, evolution, text
):
    """Structural guard: amount-only text right after a registration → asks, creates nothing."""
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    ai.set(create("EXPENSE", "80", catalog["outros_gastos"]))
    record = deliver(client, text, "M2")
    assert Transaction.objects.count() == 1
    assert code_of(record) == "CORRECTION_NOT_CREATED"


def test_elliptic_amount_without_previous_mutation_still_creates(
    client, alice, catalog, ai, evolution
):
    ai.set(create("EXPENSE", "80", catalog["outros_gastos"]))
    record = deliver(client, "80", "M1")
    assert code_of(record) == ""
    assert Transaction.objects.count() == 1


@pytest.mark.parametrize(
    "text, proposal_args",
    [
        ("Gastei 80 no mercado", ("EXPENSE", "80", "alimentacao", "mercado")),  # case 39
        ("Recebi 950 hoje", ("INCOME", "950", "salario", None)),  # case 40
        ("50 de uber", ("EXPENSE", "50", "transporte", "uber")),
    ],
)
def test_plain_create_right_after_a_create_still_works(
    client, alice, catalog, ai, evolution, text, proposal_args
):
    ai.set(create("EXPENSE", "100", catalog["alimentacao"], "mercado"))
    deliver(client, "Gastei 100 no mercado", "M1")
    kind, amount, category, description = proposal_args
    ai.set(create(kind, amount, catalog[category], description))
    record = deliver(client, text, "M2")
    assert code_of(record) == ""
    assert Transaction.objects.count() == 2


# --- Idempotency (41) -------------------------------------------------------------------------


def test_redelivered_update_and_delete_apply_once(client, alice, ledger, ai, evolution):
    """Case 41: a retry of the same provider id never re-applies UPDATE/DELETE."""
    ai.set(update(ref(transaction_type="INCOME", amount="50"), amount="40"))
    deliver(client, "a entrada de 50 era 40", "U1")
    ai.set(update(ref(transaction_type="INCOME", amount="40"), amount="30"))
    deliver(client, "a entrada de 50 era 40", "U1")  # retry (would match 40 now)
    ledger["income_50"].refresh_from_db()
    assert ledger["income_50"].amount == Decimal("40.00")

    ai.set(undo(ref(transaction_type="INCOME", amount="40")))
    deliver(client, "apague a entrada de 40", "D1")
    deliver(client, "apague a entrada de 40", "D1")
    assert TransactionChange.objects.filter(action=Action.DELETE).count() == 1
    assert TransactionChange.objects.filter(action=Action.UPDATE).count() == 1
    assert ai.call_count == 2
