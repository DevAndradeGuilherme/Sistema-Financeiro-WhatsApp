"""Every text sent to the user, built locally (never by the model). Short, pt-BR."""

from datetime import date
from decimal import Decimal

from common.formatting import describe_day, describe_period, format_brl, format_date

NEWLINE = "\n"

HELP = (
    "Você pode registrar gastos e receitas ou consultar seus valores. Exemplos:\n"
    "• 25 reais de gasolina\n"
    "• recebi 1500 de salário\n"
    "• quanto gastei esse mês?"
)
NOT_UNDERSTOOD = (
    "Não consegui entender. Você pode enviar algo como "
    "'gastei 25 de gasolina' ou 'quanto gastei esse mês?'."
)
MISSING_AMOUNT = (
    "Não identifiquei o valor. Envie de novo com o valor, por exemplo: 25 reais de gasolina."
)
INVALID_AMOUNT = "Não entendi o valor. Envie, por exemplo: 25,90 de mercado."
INVALID_DATE = "Não entendi a data. Tente, por exemplo: 'ontem' ou 'dia 5'."
INVALID_PERIOD = "Período inválido: a data inicial deve ser anterior ou igual à final."
CATEGORY_NOT_FOUND = "Não encontrei essa categoria. Tente, por exemplo: 'quanto gastei esse mês?'."
TECHNICAL_ERROR = "Não consegui processar sua mensagem agora. Tente novamente em alguns instantes."
NO_CONTEXT = "Não consegui identificar a consulta anterior. Pode repetir a pergunta completa?"
NOTHING_TO_UPDATE = "Não identifiquei o que alterar. Exemplo: 'muda o último gasto para 15 reais'."
NOT_FOUND = "Não encontrei esse lançamento. Pode informar o valor ou a data?"
NOTHING_TO_UNDO = "Não encontrei nenhuma ação recente para desfazer."
NO_CHANGE = "Nada foi alterado: o lançamento já tem esses valores."
AUDIO_TOO_LARGE = "Esse áudio é muito grande. Envie um áudio mais curto."
AUDIO_DOWNLOAD_FAILED = "Não consegui acessar esse áudio. Tente enviar novamente."
AUDIO_NOT_UNDERSTOOD = "Não consegui entender o áudio. Pode repetir ou enviar por texto?"
AUDIO_UNSUPPORTED = "Não consegui processar esse formato de áudio. Envie uma mensagem de voz."
NOT_A_RECEIPT = "Não consegui identificar um comprovante ou recibo nessa imagem."
RECEIPT_AMOUNT_UNREADABLE = (
    "Identifiquei um comprovante, mas não consegui ler o valor com segurança."
)
RECEIPT_FOREIGN_CURRENCY = (
    "Identifiquei um valor em outra moeda, mas essa versão registra apenas valores em reais."
)
RECEIPT_CURRENCY_UNCLEAR = (
    "Identifiquei um comprovante, mas não consegui confirmar que o valor está em reais."
)
RECEIPT_DIRECTION_UNCLEAR = (
    "Esse comprovante é de um valor pago ou recebido? Envie em texto, por exemplo: "
    "'paguei 50 reais' ou 'recebi 50 reais'."
)
RECEIPT_DATE_UNREADABLE = "Identifiquei um comprovante, mas não consegui ler a data com segurança."
IMAGE_TOO_LARGE = "Essa imagem é muito grande. Envie uma foto ou print menor."
IMAGE_INVALID = "Não consegui abrir essa imagem. Envie uma foto ou print em JPG ou PNG."
IMAGE_DOWNLOAD_FAILED = "Não consegui acessar essa imagem. Tente enviar novamente."
RECEIPT_STORED_SUFFIX = " Comprovante armazenado."
ADMIN_ONLY = "Essa consulta está disponível apenas para o administrador da família."
NO_FAMILY = "Você ainda não faz parte de uma família cadastrada."
MEMBER_NOT_FOUND = "Não encontrei esse membro na sua família."
OWN_ACCOUNT_ONLY = "Você só pode registrar transações na sua própria conta."
OWN_CHANGES_ONLY = "Você só pode alterar suas próprias transações."

# ``who`` in the functions below: None = the sender ("Você"), FAMILY = the whole family,
# any other string = a family member's display name.
FAMILY = "__family__"


def member_ambiguous(name: str | None) -> str:
    who = f" chamado {name}" if name else ""
    return f"Encontrei mais de um membro{who} na família. Pode informar o nome completo?"


def _subject(who: str | None) -> str:
    return "Você" if who is None else ("A família" if who == FAMILY else who)


def _of(who: str | None) -> str:
    return "" if who is None else (" da família" if who == FAMILY else f" de {who}")


def rejected(reason: str) -> str:
    return f"Não registrei: {reason}"


def transaction_created(
    *,
    is_income: bool,
    amount: Decimal,
    category: str,
    subcategory: str | None,
    transaction_date: date,
    today: date,
) -> str:
    label = "Entrada registrada" if is_income else "Gasto registrado"
    where = f"{category} · {subcategory}" if subcategory else category
    when = "" if transaction_date == today else f" ({describe_day(transaction_date, today)})"
    return f"✅ {label}: {format_brl(amount)} em {where}{when}."


def total(
    *,
    is_income: bool,
    value: Decimal,
    scope: str | None,
    start: date,
    end: date,
    today: date,
    who: str | None = None,
) -> str:
    verb = "recebeu" if is_income else "gastou"
    preposition = "em" if is_income else "com"
    scope_text = f" {preposition} {scope}" if scope else ""
    period = describe_period(start, end, today)
    return f"{_subject(who)} {verb} {format_brl(value)}{scope_text} {period}."


def balance(
    *,
    income: Decimal,
    expense: Decimal,
    start: date,
    end: date,
    today: date,
    who: str | None = None,
) -> str:
    period = describe_period(start, end, today)
    owner = "Seu saldo" if who is None else f"O saldo{_of(who)}"
    return (
        f"{owner} {period} é {format_brl(income - expense)} "
        f"(entradas {format_brl(income)}; gastos {format_brl(expense)})."
    )


def _label(is_income: bool) -> str:
    return "entrada" if is_income else "gasto"


def where(category: str, subcategory: str | None) -> str:
    return f"{category} · {subcategory}" if subcategory else category


def ambiguous(*, is_income: bool | None, description: str | None) -> str:
    noun = {True: "entrada", False: "gasto", None: "lançamento"}[is_income]
    about = f" de {description}" if description else ""
    return f"Encontrei mais de um {noun}{about}. Pode me dizer o valor ou a data?"


def transaction_updated(
    *,
    is_income: bool,
    old_amount: Decimal,
    new_amount: Decimal,
    old_where: str,
    new_where: str,
    new_date: date | None,
    new_description: str | None,
) -> str:
    amount = (
        f"{format_brl(old_amount)} → {format_brl(new_amount)}"
        if old_amount != new_amount
        else format_brl(new_amount)
    )
    text = f"✏️ {_label(is_income).capitalize()} atualizado: {amount} em {new_where}"
    if old_where != new_where:
        text += f" (antes: {old_where})"
    if new_date is not None:
        text += f", data {format_date(new_date)}"
    text += "."
    if new_description is not None:
        text += f" Descrição: {new_description}." if new_description else " Descrição removida."
    return text


def undo_create(*, amount: Decimal, where: str) -> str:
    return f"↩️ Registro de {format_brl(amount)} em {where} desfeito."


def undo_update(*, is_income: bool, amount: Decimal, where: str) -> str:
    article = "A entrada" if is_income else "O gasto"
    return f"↩️ Alteração desfeita. {article} voltou para {format_brl(amount)} em {where}."


def ranked(title: str, rows: list[tuple[str, Decimal]]) -> str:
    lines = [
        f"{index}. {label} — {format_brl(value)}" for index, (label, value) in enumerate(rows, 1)
    ]
    return NEWLINE.join([title, *lines])


def top_expenses(
    rows: list[tuple[str, Decimal]],
    *,
    start: date,
    end: date,
    today: date,
    who: str | None = None,
) -> str:
    period = describe_period(start, end, today)
    if not rows:
        return f"Não encontrei gastos{_of(who)} {period}."
    if who is None:
        return ranked(f"Seus maiores gastos {period}:", rows)
    return ranked(f"Maiores gastos{_of(who)} {period}:", rows)


def breakdown(
    rows: list[tuple[str, Decimal]],
    *,
    is_income: bool,
    start: date,
    end: date,
    today: date,
    who: str | None = None,
) -> str:
    period = describe_period(start, end, today)
    noun = "receitas" if is_income else "gastos"
    if not rows:
        return f"Não encontrei {noun}{_of(who)} {period}."
    return ranked(f"{noun.capitalize()}{_of(who)} por categoria {period}:", rows)


def ranking(
    rows: list[tuple[str, Decimal]], *, is_income: bool, start: date, end: date, today: date
) -> str:
    period = describe_period(start, end, today)
    noun = "receitas" if is_income else "gastos"
    if not rows:
        return f"Não encontrei {noun} da família {period}."
    return ranked(f"{noun.capitalize()} por pessoa {period}:", rows)


def listing(
    rows: list[tuple[date, str, Decimal, bool]],
    *,
    type_: str | None,
    period: tuple[date, date] | None,
    today: date,
    who: str | None = None,
) -> str:
    noun = {"EXPENSE": "gastos", "INCOME": "entradas", None: "lançamentos"}[type_]
    when = describe_period(*period, today) if period else None
    if not rows:
        return f"Não encontrei {noun}{_of(who)}" + (f" {when}." if when else ".")
    if who is None:
        title = f"Seus {noun} {when}:" if when else f"Seus últimos {noun}:"
    else:
        title = f"{noun.capitalize()}{_of(who)} {when}:" if when else f"Últimos {noun}{_of(who)}:"
    lines = [
        f"{index}. {day:%d/%m} — {label} — {'+' if is_income and type_ is None else ''}"
        f"{format_brl(value)}"
        for index, (day, label, value, is_income) in enumerate(rows, 1)
    ]
    return NEWLINE.join([title, *lines])


def comparison(
    *,
    is_income: bool,
    current: Decimal,
    previous: Decimal,
    percent: Decimal | None,
    scope: str | None,
    period_a: tuple[date, date],
    period_b: tuple[date, date],
    today: date,
    who: str | None = None,
) -> str:
    verb = "recebeu" if is_income else "gastou"
    scope_text = f" {'em' if is_income else 'com'} {scope}" if scope else ""
    a = describe_period(*period_a, today)
    b = describe_period(*period_b, today)
    text = (
        f"{_subject(who)} {verb} {format_brl(current)}{scope_text} {a} e {format_brl(previous)} {b}"
    )
    difference = current - previous
    if difference == 0:
        return text + ": o mesmo valor."
    direction = "a mais" if difference > 0 else "a menos"
    text += f": {format_brl(abs(difference))} {direction}"
    if percent is None:
        nothing = "nada recebido" if is_income else "sem gastos"
        return text + f" ({nothing} no período anterior)."
    sign = "+" if percent > 0 else ""
    return text + f" ({sign}{str(percent).replace('.', ',')}%)."
