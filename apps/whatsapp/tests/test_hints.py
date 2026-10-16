"""Deterministic text reading used as defense in depth for UPDATE/UNDO (no database)."""

from decimal import Decimal

import pytest

from apps.whatsapp import hints, pending


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Desfaça a entrada de 50", dict(type="INCOME", amount=True, date=False, bulk=False)),
        ("Desfaça a entrada do dia 07/10", dict(type="INCOME", amount=False, date=True)),
        ("Remova a entrada de R$ 50 do dia 07/10", dict(type="INCOME", amount=True, date=True)),
        ("desfaz a entrada de 7 de outubro", dict(amount=False, date=True)),
        ("apague o gasto de outubro de 2025", dict(type="EXPENSE", amount=False, date=True)),
        ("Desfaça todas as entradas", dict(type="INCOME", bulk=True)),
        ("remova todos os gastos desse mês", dict(type="EXPENSE", bulk=True, date=True)),
        ("exclua os gastos de ontem", dict(bulk=True, date=True)),
        ("apague tudo de alimentação", dict(type=None, bulk=True)),
        ("remova essas entradas", dict(bulk=True)),
        ("Desfaça o último gasto", dict(type="EXPENSE", last=True, bulk=False)),
        ("Desfaça a última entrada", dict(type="INCOME", last=True)),
        ("apaga o que acabei de registrar", dict(last=True, type=None)),
        ("desfaz a última alteração", dict(last=True, action=True)),
        ("desfazer", dict(type=None, amount=False, date=False, bulk=False, last=False)),
        ("troca o gasto pela entrada", dict(type=None)),
    ],
)
def test_reference_hints(text, expected):
    found = hints.read(text)
    for name, value in expected.items():
        assert getattr(found, name) == value, name


@pytest.mark.parametrize(
    "text",
    [
        "Errado, recebi somente 1.050,41",
        "errado foram 80",
        "Na verdade foi 950",
        "Era 50",
        "corrige isso",
        "corrija a última",
        "não foi 100, foi 80",
        "quis dizer 80",
        "o valor certo é 30",
    ],
)
def test_correction_markers(text):
    assert hints.read(text).correction is True


@pytest.mark.parametrize(
    "text",
    [
        "Gastei 100 no mercado",
        "Recebi 950 hoje",
        "gastei só 10 no café",
        "paguei a operadora",
        "comprei uma era de jogos",
    ],
)
def test_plain_messages_are_not_corrections(text):
    assert hints.read(text).correction is False


@pytest.mark.parametrize(
    "text, expected",
    [
        ("sim", pending.CONFIRM),
        ("Sim!", pending.CONFIRM),
        ("SIM, pode apagar", pending.CONFIRM),
        ("confirmo", pending.CONFIRM),
        ("pode remover.", pending.CONFIRM),
        ("não", pending.DENY),
        ("Nao!", pending.DENY),
        ("cancela", pending.DENY),
        ("sim, mas só as de outubro", None),
        ("simples", None),
        ("apague todas as entradas", None),
        ("", None),
        (None, None),
    ],
)
def test_confirmation_decision_is_whole_message(text, expected):
    assert pending.decision(text) == expected


# --- Audit P2#2: corrective phrases and elliptic messages ------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Desculpa, foram 80",
        "Não, foram 80",
        "Foi 80, não 100",
        "foram 80 reais, não 100",
        "na real foram 80",
        "me enganei, era 80",
        "corrigindo: 80",
        "o certo é 80",
        "foram 80",
        "não, 80",
    ],
)
def test_more_correction_markers(text):
    assert hints.read(text).correction is True


@pytest.mark.parametrize(
    "text",
    [
        "Desculpa, gastei 50 no mercado",
        "Gastei 80 no mercado",
        "Recebi 950 hoje",
        "o mercado foi 80",
        "80",
        "50 de uber",
    ],
)
def test_creations_are_not_corrections(text):
    assert hints.read(text).correction is False


@pytest.mark.parametrize(
    "text, elliptic",
    [
        ("80", True),
        ("R$ 80,00", True),
        ("80 reais", True),
        ("ok, 80", True),
        ("Desculpa, foram 80", True),
        ("Foi 80, não 100", True),
        ("Gastei 80 no mercado", False),
        ("Recebi 950 hoje", False),
        ("50 de uber", False),
        ("errado", False),  # no amount
        ("80 dia 07/10", False),  # a date describes a transaction
    ],
)
def test_elliptic_messages(text, elliptic):
    assert hints.read(text).elliptic is elliptic


@pytest.mark.parametrize(
    "text, amounts",
    [
        ("remova a entrada de 50", (Decimal("50"),)),
        ("Errado, recebi somente 1.050,41", (Decimal("1050.41"),)),
        ("R$ 10.50 de café", (Decimal("10.50"),)),
        ("paguei 1.500 de aluguel", (Decimal("1500"),)),
        ("a entrada de 50 do dia 07/10 era 40", (Decimal("50"), Decimal("40"))),
        ("o gasto de 7 de outubro", ()),
        ("o gasto do dia 5", ()),
    ],
)
def test_amounts_are_read_outside_dates(text, amounts):
    assert hints.read(text).amounts == amounts


def test_explicit_days():
    found = hints.read("apague a entrada de 50 do dia 07/10 e a de 01/02/2025 de ontem")
    assert found.day_months == ((7, 10, None), (1, 2, 2025))
    assert found.days_ago == (1,)
    assert hints.read("anteontem").days_ago == (2,)
    assert hints.read("hoje").days_ago == (0,)


def test_find_terms_whole_words_longest_first():
    terms = {"transporte", "transporte publico", "uber", "ana"}
    assert hints.find_terms("apague o Transporte Público", terms) == {"transporte publico"}
    assert hints.find_terms("o uber da Ana", terms) == {"uber", "ana"}
    assert hints.find_terms("banana", terms) == set()


@pytest.mark.parametrize("text", ["desfazer", "remove", "apaga", "cancela", "desfaz isso"])
def test_bare_verbs_identify_nothing(text):
    found = hints.read(text)
    assert (found.type, found.amounts, found.date, found.last, found.bulk) == (
        None,
        (),
        False,
        False,
        False,
    )


@pytest.mark.parametrize(
    "text",
    [
        "tira o gasto de 30",
        "tira esse gasto de 30",
        "tira a entrada de ontem",
        "retira esse lançamento",
        "retirar a última despesa",
        "estorna o gasto de 50",
        "estornar essa receita",
        "tire o registro de ontem",
    ],
)
def test_colloquial_removal_of_a_registration_is_remove(text):
    assert hints.read(text).mutation == "REMOVE"


@pytest.mark.parametrize(
    "text",
    [
        "comprei tira-gosto por 30",
        "paguei 100 para tirar uma tatuagem",
        "retirei 200 no caixa",
        "tirei uma foto",
        "troca de óleo",
        "estornaram 50 no cartão",
    ],
)
def test_colloquial_verbs_without_a_registration_are_not_remove(text):
    assert hints.read(text).mutation is None
