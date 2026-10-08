"""Reply texts that the pipeline tests only read by content: the card layout and edge phrasings."""

from datetime import date
from decimal import Decimal

from apps.whatsapp import messages

TODAY = date(2026, 10, 8)
OCTOBER = (date(2026, 10, 1), TODAY)


def test_expense_card_layout():
    assert messages.transaction_created(
        is_income=False,
        amount=Decimal("35.00"),
        category="Transporte",
        subcategory="Combustível",
        transaction_date=TODAY,
    ) == (
        "✅ Gasto registrado\n"
        "\n"
        "💲 Valor: R$ 35,00\n"
        "🗂️ Categoria: Transporte\n"
        "🏷️ Subcategoria: Combustível\n"
        "🗓️ Data: 08/10/2026"
    )


def test_income_card_omits_missing_subcategory():
    assert messages.transaction_created(
        is_income=True,
        amount=Decimal("1500.00"),
        category="Salário",
        subcategory=None,
        transaction_date=TODAY,
    ) == (
        "✅ Receita registrada\n\n💲 Valor: R$ 1.500,00\n🗂️ Categoria: Salário\n🗓️ Data: 08/10/2026"
    )


def test_zero_total_reads_as_no_expenses():
    text = messages.total(
        is_income=False, value=Decimal("0"), scope="Lazer", start=OCTOBER[0], end=TODAY, today=TODAY
    )
    assert text == "Você não teve gastos com Lazer em outubro."


def test_negative_balance_is_spelled_out():
    text = messages.balance(
        income=Decimal("100.00"),
        expense=Decimal("350.00"),
        start=OCTOBER[0],
        end=TODAY,
        today=TODAY,
    )
    assert text.startswith("Seu saldo em outubro ficou negativo em R$ 250,00.")
    assert "-R$" not in text


def test_balance_without_movements():
    text = messages.balance(
        income=Decimal("0"), expense=Decimal("0"), start=OCTOBER[0], end=TODAY, today=TODAY
    )
    assert text == "Não encontrei receitas nem gastos em outubro."


def test_update_lists_every_change():
    text = messages.transaction_updated(
        is_income=False,
        old_amount=Decimal("10.00"),
        new_amount=Decimal("15.00"),
        old_where="Transporte",
        new_where="Alimentação",
        new_date=date(2026, 10, 7),
        new_description="",
    )
    assert text == (
        "Pronto, alterei esse gasto: mudei o valor para R$ 15,00, a categoria para Alimentação "
        "e a data para 07/10/2026 e removi a descrição."
    )


def test_ambiguous_member_agrees_with_pessoa():
    assert "uma pessoa chamada Ana" in messages.member_ambiguous("Ana")
