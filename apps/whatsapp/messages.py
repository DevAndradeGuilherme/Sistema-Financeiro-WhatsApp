"""Every text sent to the user, built locally (never by the model). Conversational pt-BR.

Only the "transaction created" confirmation is a structured card; everything else reads like a
short chat message. Numbers come ready from selectors/analytics: this module only formats them.
"""

from datetime import date
from decimal import Decimal

from common.formatting import MONTHS, describe_period, format_brl, format_date

NEWLINE = "\n"

HELP = (
    "Você pode registrar gastos e receitas ou consultar seus valores. Por exemplo:\n\n"
    "• 25 reais de gasolina\n"
    "• recebi 1500 de salário\n"
    "• quanto gastei esse mês?"
)
NOT_UNDERSTOOD = (
    "Não consegui entender. Pode tentar escrever de outra forma? Por exemplo: "
    "'gastei 25 de gasolina' ou 'quanto gastei esse mês?'."
)
MISSING_AMOUNT = "Faltou o valor. Pode mandar de novo com ele? Por exemplo: 25 reais de gasolina."
INVALID_AMOUNT = "Não entendi o valor. Pode mandar de novo? Por exemplo: 25,90 de mercado."
INVALID_DATE = "Não entendi a data. Pode tentar algo como 'ontem' ou 'dia 5'?"
INVALID_PERIOD = "Esse período não fecha: a data inicial precisa vir antes da final."
CATEGORY_NOT_FOUND = "Não encontrei essa categoria. Pode tentar com outras palavras?"
TECHNICAL_ERROR = "Não consegui processar sua mensagem agora. Tente de novo em alguns instantes."
NO_CONTEXT = "Não sei a qual consulta você se refere. Pode repetir a pergunta completa?"
NOTHING_TO_UPDATE = (
    "Não entendi o que você quer alterar. Pode dizer algo como 'muda o último gasto para 15 reais'?"
)
NOT_FOUND = (
    "Não encontrei nenhum lançamento recente que corresponda a isso. "
    "Pode me dizer o valor ou a data?"
)
NOTHING_TO_UNDO = "Não encontrei nenhuma ação recente para desfazer."
NO_CHANGE = "Esse lançamento já está assim, então não precisei mudar nada."
AUDIO_TOO_LARGE = "Esse áudio ficou muito longo. Pode mandar um mais curto?"
AUDIO_DOWNLOAD_FAILED = "Não consegui processar esse áudio. Tente enviar novamente."
AUDIO_NOT_UNDERSTOOD = "Não consegui entender esse áudio. Pode repetir ou mandar por texto?"
AUDIO_UNSUPPORTED = "Não consegui abrir esse áudio. Pode mandar como mensagem de voz?"
NOT_A_RECEIPT = "Não consegui identificar um comprovante ou recibo nessa imagem."
RECEIPT_AMOUNT_UNREADABLE = (
    "Não consegui ler o valor desse comprovante com segurança. Pode me mandar o valor por texto?"
)
RECEIPT_FOREIGN_CURRENCY = (
    "Esse comprovante está em outra moeda, e por enquanto só registro valores em reais."
)
RECEIPT_CURRENCY_UNCLEAR = (
    "Não consegui confirmar se o valor desse comprovante está em reais. Pode me mandar por texto?"
)
RECEIPT_DIRECTION_UNCLEAR = (
    "Esse comprovante é de um valor que você pagou ou recebeu? Me conte por texto, por exemplo: "
    "'paguei 50 reais' ou 'recebi 50 reais'."
)
RECEIPT_DATE_UNREADABLE = "Não consegui ler a data desse comprovante com segurança."
IMAGE_TOO_LARGE = "Essa imagem é muito grande. Pode mandar uma foto ou print menor?"
IMAGE_INVALID = "Não consegui abrir essa imagem. Pode mandar uma foto ou print em JPG ou PNG?"
IMAGE_DOWNLOAD_FAILED = "Não consegui processar essa imagem. Tente enviar novamente."
ADMIN_ONLY = "Essa consulta está disponível apenas para o administrador da família."
NO_FAMILY = "Você ainda não faz parte de uma família cadastrada."
MEMBER_NOT_FOUND = "Não encontrei essa pessoa na sua família."
OWN_ACCOUNT_ONLY = (
    "Só consigo registrar lançamentos na sua própria conta. Se foi você quem pagou, mande por "
    'exemplo "paguei 100 do mercado". Se foi outra pessoa, ela mesma precisa registrar.'
)
OWN_CHANGES_ONLY = (
    "Só consigo alterar ou remover lançamentos feitos por você. Lançamentos de outro membro "
    "da família não podem ser alterados nem removidos por você, então não mudei nada."
)

# --- Target selection, corrections and confirmations (UPDATE/UNDO) ---------------------------

CORRECTION_TARGET_UNCLEAR = (
    "Qual lançamento você quer corrigir? Me diga o valor e a data, por exemplo: "
    "'a entrada de 50 do dia 07/10 era 40'."
)
CORRECTION_NOT_CREATED = (
    "Parece que você quer corrigir um lançamento, então não registrei nada novo. "
    + CORRECTION_TARGET_UNCLEAR
)
REMOVE_TARGET_UNCLEAR = (
    "Não consegui identificar com segurança qual lançamento você quer remover, então não "
    "removi nada. Me diga o valor e a data, por exemplo: 'remova a entrada de 50 do dia 07/10' "
    "ou 'desfaça o último lançamento'."
)
REMOVE_NOT_CREATED = (
    "Parece que você quer remover um lançamento, então não registrei nada novo. "
    + REMOVE_TARGET_UNCLEAR
)
BULK_UPDATE_UNSUPPORTED = (
    "Por segurança, só altero um lançamento por vez. Me diga qual, por exemplo: "
    "'o gasto de 50 do dia 07/10 era 40'."
)
BULK_NEEDS_CRITERIA = (
    "Para remover vários lançamentos de uma vez, me diga o tipo, o período ou a categoria. "
    "Por exemplo: 'apague as entradas de outubro'."
)
NO_PENDING_ACTION = "Não há nenhuma ação aguardando confirmação."
CONFIRMATION_CANCELLED = "Tudo bem, não removi nada."
CONFIRMATION_EXPIRED = (
    "Esse pedido de confirmação expirou, então não removi nada. Se ainda quiser, peça de novo."
)
CONFIRMATION_STALE = (
    "Esses lançamentos mudaram desde o pedido, então não removi nada. Se ainda quiser, "
    "peça de novo."
)
CONFIRM_HINT = "Responda SIM para confirmar ou NÃO para cancelar."

_SINGULAR = {"INCOME": "entrada", "EXPENSE": "gasto", None: "lançamento"}
_PLURAL = {"INCOME": "receitas", "EXPENSE": "gastos", None: "lançamentos"}


def _target_details(
    *,
    amount: Decimal | None = None,
    category: str | None = None,
    description: str | None = None,
    when: str | None = None,
) -> str:
    text = f" de {format_brl(amount)}" if amount is not None else ""
    if category:
        text += f" em {category}"
    if description:
        text += f" com “{description}”"
    if when:
        text += f" {when}"
    return text


def target_not_found(type_: str | None, **details) -> str:
    article = "uma" if type_ == "INCOME" else "um"
    return f"Não encontrei {article} {_SINGULAR[type_]}{_target_details(**details)}."


def remove_which(type_: str) -> str:
    """Only a type was given ("desfaça a entrada"): it identifies nothing."""
    noun = _SINGULAR[type_]
    which = "Qual entrada" if type_ == "INCOME" else "Qual gasto"
    last = "a última entrada" if type_ == "INCOME" else "o último gasto"
    return (
        f"{which} você quer remover? Não removi nada. Me diga o valor ou a data, por exemplo: "
        f"'desfaça {'a' if type_ == 'INCOME' else 'o'} {noun} de 50 de ontem' ou "
        f"'desfaça {last}'."
    )


def transaction_removed(*, is_income: bool, amount: Decimal, day: date, where: str) -> str:
    noun = "a entrada" if is_income else "o gasto"
    return f"Pronto, removi {noun} de {format_brl(amount)} de {day:%d/%m} ({where})."


def ambiguous_target(candidates: list[tuple[bool, Decimal, date, str]], *, more: bool, verb: str):
    """``candidates``: (is_income, amount, transaction_date, label). ``verb``: remover/corrigir."""
    rows = [
        f"• {'Entrada' if income else 'Gasto'} de {format_brl(amount)} em {day:%d/%m} ({label})"
        for income, amount, day, label in candidates
    ]
    if more:
        rows.append("• …e outros")
    return _lines(
        f"Encontrei mais de uma transação correspondente. Qual delas você quer {verb}?",
        rows,
        "Me diga o valor, a data ou a descrição para eu saber qual é.",
    )


def confirm_bulk_delete(*, type_: str | None, count: int, total: Decimal, **details) -> str:
    feminine = type_ == "INCOME"
    if count == 1:
        noun = {"INCOME": "receita", "EXPENSE": "gasto", None: "lançamento"}[type_]
        found = f"Encontrei 1 {noun}{_target_details(**details)}, de {format_brl(total)}."
        question = f"Deseja realmente {'removê-la' if feminine else 'removê-lo'}?"
    else:
        found = (
            f"Encontrei {count} {_PLURAL[type_]}{_target_details(**details)}, "
            f"totalizando {format_brl(total)}."
        )
        question = f"Deseja realmente remover {'todas' if feminine else 'todos'}?"
    return f"{found} {question} {CONFIRM_HINT}"


def bulk_too_many(*, limit: int) -> str:
    return (
        f"Encontrei mais de {limit} lançamentos, mais do que consigo remover de uma vez. "
        "Pode restringir por período ou categoria?"
    )


def bulk_deleted(*, type_: str | None, count: int, total: Decimal) -> str:
    if count == 1:
        return f"Pronto, removi 1 {_SINGULAR[type_]} de {format_brl(total)}."
    return f"Pronto, removi {count} {_PLURAL[type_]}, totalizando {format_brl(total)}."


# ``who`` in the functions below: None = the sender ("Você"), FAMILY = the whole family,
# any other string = a family member's display name.
FAMILY = "__family__"


def member_ambiguous(name: str | None) -> str:
    who = f" chamada {name}" if name else ""
    return f"Encontrei mais de uma pessoa{who} na família. Pode me dizer o nome completo?"


def _subject(who: str | None) -> str:
    return "Você" if who is None else ("Sua família" if who == FAMILY else who)


def _of(who: str | None) -> str:
    return "" if who is None else (" da sua família" if who == FAMILY else f" de {who}")


def _their(noun: str, who: str | None, *, feminine: bool = False) -> str:
    """'seus gastos' / 'os gastos da sua família' / 'as receitas de Maria'."""
    if who is None:
        return f"{'suas' if feminine else 'seus'} {noun}"
    return f"{'as' if feminine else 'os'} {noun}{_of(who)}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _pct(value: Decimal, *, signed: bool = False) -> str:
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{str(value).replace('.', ',')}%"


def _signed_brl(value: Decimal) -> str:
    return f"+{format_brl(value)}" if value > 0 else format_brl(value)


def _join(parts: list[str]) -> str:
    """['a', 'b', 'c'] → 'a, b e c'."""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} e {parts[-1]}"


def _lines(intro: str, rows: list[str], outro: str | None = None) -> str:
    blocks = [intro, NEWLINE.join(rows)]
    if outro:
        blocks.append(outro)
    return "\n\n".join(blocks)


def _relative_to(period: str) -> str:
    """'em setembro' → 'a setembro'; 'de 01/09/2026 a 15/09/2026' → 'ao período de ...'."""
    if period.startswith("em "):
        return f"a {period.removeprefix('em ')}"
    if period in ("hoje", "ontem"):
        return f"a {period}"
    return f"ao período {period}"


def rejected(reason: str) -> str:
    return f"Não consegui salvar: {reason}"


def transaction_created(
    *,
    is_income: bool,
    amount: Decimal,
    category: str,
    subcategory: str | None,
    transaction_date: date,
) -> str:
    title = "✅ Receita registrada" if is_income else "✅ Gasto registrado"
    lines = [f"💲 Valor: {format_brl(amount)}", f"🗂️ Categoria: {category}"]
    if subcategory:
        lines.append(f"🏷️ Subcategoria: {subcategory}")
    lines.append(f"🗓️ Data: {format_date(transaction_date)}")
    return _lines(title, lines)


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
    preposition = "em" if is_income else "com"
    scope_text = f" {preposition} {scope}" if scope else ""
    period = describe_period(start, end, today)
    if value == 0:
        noun = "receitas" if is_income else "gastos"
        return f"{_subject(who)} não teve {noun}{scope_text} {period}."
    verb = "recebeu" if is_income else "gastou"
    return f"{_subject(who)} {verb} {format_brl(value)}{scope_text} {period}."


def _balance_phrase(owner: str, value: Decimal) -> str:
    if value < 0:
        return f"{owner} ficou negativo em {format_brl(-value)}"
    return f"{owner} ficou em {format_brl(value)}"


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
    if income == 0 and expense == 0:
        return f"Não encontrei receitas nem gastos{_of(who)} {period}."
    owner = "Seu saldo" if who is None else f"O saldo{_of(who)}"
    return (
        f"{_balance_phrase(f'{owner} {period}', income - expense)}. "
        f"Entraram {format_brl(income)} e saíram {format_brl(expense)}."
    )


def where(category: str, subcategory: str | None) -> str:
    return f"{category} · {subcategory}" if subcategory else category


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
    target = "essa receita" if is_income else "esse gasto"
    changes = []
    if old_amount != new_amount:
        changes.append(f"o valor para {format_brl(new_amount)}")
    if old_where != new_where:
        changes.append(f"a categoria para {new_where}")
    if new_date is not None:
        changes.append(f"a data para {format_date(new_date)}")
    if new_description:
        changes.append(f"a descrição para “{new_description}”")
    removed = new_description == ""

    if changes == [f"o valor para {format_brl(new_amount)}"] and not removed:
        return f"Pronto, alterei {target} para {format_brl(new_amount)}."
    actions = [f"mudei {_join(changes)}"] if changes else []
    if removed:
        actions.append("removi a descrição")
    if not actions:
        return f"Pronto, alterei {target}."
    return f"Pronto, alterei {target}: {' e '.join(actions)}."


def undo_create(*, amount: Decimal, where: str) -> str:
    return f"Pronto, removi o último lançamento ({format_brl(amount)} em {where})."


def undo_update(*, is_income: bool, amount: Decimal, where: str) -> str:
    article = "A receita" if is_income else "O gasto"
    return (
        f"Pronto, desfiz a última alteração. {article} voltou para {format_brl(amount)} em {where}."
    )


def _rows(rows: list[tuple[str, Decimal]]) -> list[str]:
    return [f"{label}: {format_brl(value)}" for label, value in rows]


def top_expenses(
    rows: list[tuple[str, Decimal]],
    *,
    start: date,
    end: date,
    today: date,
    who: str | None = None,
    is_income: bool = False,
) -> str:
    period = describe_period(start, end, today)
    noun = "receitas" if is_income else "gastos"
    if not rows:
        return f"Não encontrei {noun}{_of(who)} {period}."
    if len(rows) == 1:
        label, value = rows[0]
        if who is None:
            title = "Sua maior receita" if is_income else "Seu maior gasto"
        else:
            title = ("A maior receita" if is_income else "O maior gasto") + _of(who)
        return f"{title} {period} foi {label}, de {format_brl(value)}."
    biggest = _their(f"maiores {noun}", who, feminine=is_income)
    return _lines(f"{_cap(biggest)} {period} foram:", _rows(rows))


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
    return _lines(
        f"{_cap(period)}, {_their(noun, who, feminine=is_income)} por categoria foram:",
        _rows(rows),
    )


def ranking(
    rows: list[tuple[str, Decimal]], *, is_income: bool, start: date, end: date, today: date
) -> str:
    period = describe_period(start, end, today)
    noun = "receitas" if is_income else "gastos"
    if not rows:
        return f"Não encontrei {noun} da sua família {period}."
    return _lines(
        f"{_cap(period)}, {_their(noun, FAMILY, feminine=is_income)} por pessoa foram:",
        _rows(rows),
    )


def listing(
    rows: list[tuple[date, str, Decimal, bool]],
    *,
    type_: str | None,
    period: tuple[date, date] | None,
    today: date,
    who: str | None = None,
) -> str:
    noun = {"EXPENSE": "gastos", "INCOME": "receitas", None: "lançamentos"}[type_]
    feminine = type_ == "INCOME"
    when = describe_period(*period, today) if period else None
    if not rows:
        return f"Não encontrei {noun}{_of(who)}" + (f" {when}." if when else ".")
    demonstrative = "Estas" if feminine else "Estes"
    if when:
        intro = f"{demonstrative} foram {_their(noun, who, feminine=feminine)} {when}:"
    else:
        recent = "últimas" if feminine else "últimos"
        intro = f"{demonstrative} são {_their(f'{recent} {noun}', who, feminine=feminine)}:"
    lines = [
        f"{label}: {format_brl(value)} ({day:%d/%m}{', receita' if income and not type_ else ''})"
        for day, label, value, income in rows
    ]
    return _lines(intro, lines)


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
    text = f"{_subject(who)} {verb} {format_brl(current)}{scope_text} {a}"
    text += f" e {format_brl(previous)} {b}."
    difference = current - previous
    if difference == 0:
        return text + " Foi o mesmo valor nos dois períodos."
    direction = "a mais" if difference > 0 else "a menos"
    text += f" Foram {format_brl(abs(difference))} {direction}"
    if percent is None:
        nothing = "receitas" if is_income else "gastos"
        return text + f", já que não houve {nothing} no período anterior."
    return text + f" ({_pct(percent, signed=True)})."


# --- V1.5 analytics (facts only, no advice) ------------------------------------------------------

NO_DATA = "Não encontrei movimentações suficientes nesse período para montar o resumo."


def period_summary(summary, *, start: date, end: date, today: date, who: str | None = None) -> str:
    if not summary.has_data:
        return NO_DATA
    period = _cap(describe_period(start, end, today))
    subject = "você" if who is None else ("sua família" if who == FAMILY else who)
    income, expense = summary.income_total, summary.expense_total
    if income and expense:
        opening = (
            f"{period}, entraram {format_brl(income)} e {subject} gastou {format_brl(expense)}."
        )
    elif income:
        opening = f"{period}, entraram {format_brl(income)} e não houve gastos."
    else:
        opening = f"{period}, {subject} gastou {format_brl(expense)} e não houve receitas."

    owner = "Seu saldo" if who is None else f"O saldo{_of(who)}"
    closing = _balance_phrase(f"{owner} no período", summary.balance)
    if summary.expense_ratio is not None and expense:
        closing += f", com {_pct(summary.expense_ratio)} da renda comprometida"
    paragraphs = [opening, closing + "."]

    highlights = []
    if summary.top_categories:
        top = summary.top_categories[0]
        highlights.append(
            f"A categoria com mais gastos foi {top.category_name}, com {format_brl(top.total)}."
        )
    if summary.largest_expense:
        e = summary.largest_expense
        highlights.append(f"O maior gasto foi {e.label}, de {format_brl(e.amount)}.")
    if highlights:
        paragraphs.append(" ".join(highlights))
    return "\n\n".join(paragraphs)


def monthly_trend(trend, *, today: date, capped: bool = False, who: str | None = None) -> str:
    if not trend.has_data:
        return NO_DATA
    if trend.metric == "BALANCE":
        subject, verb = ("seu saldo" if who is None else f"o saldo{_of(who)}"), "foi"
    elif trend.metric == "INCOME":
        subject, verb = _their("receitas", who, feminine=True), "foram"
    else:
        subject, verb = _their("gastos", who), "foram"
    count = len(trend.points)
    window = "No último mês" if count == 1 else f"Nos últimos {count} meses"
    first, last = trend.points[0].month, trend.points[-1].month
    if last != today.replace(day=1):
        end_label = f"{MONTHS[last.month - 1]} de {last.year}"
        window = (
            f"Em {end_label}"
            if count == 1
            else f"De {MONTHS[first.month - 1]} de {first.year} a {end_label}"
        )
    if capped:
        window += " (o máximo que consigo mostrar)"
    years = {p.month.year for p in trend.points}
    rows = []
    for p in trend.points:
        label = _cap(MONTHS[p.month.month - 1])
        if len(years) > 1 or p.month.year != today.year:
            label += f" de {p.month.year}"
        rows.append(f"{label}: {format_brl(p.value(trend.metric))}")
    return _lines(
        f"{window}, {subject} {verb}:",
        rows,
        f"A média mensal ficou em {format_brl(trend.average)}.",
    )


def category_trend(
    changes,
    *,
    direction: str,
    is_income: bool,
    period_a: tuple[date, date],
    period_b: tuple[date, date],
    today: date,
    who: str | None = None,
) -> str:
    noun = "receitas" if is_income else "gastos"
    a = describe_period(*period_a, today)
    relative = _relative_to(describe_period(*period_b, today))
    if not changes:
        if direction in ("INCREASE", "DECREASE"):
            verb = "aumentou" if direction == "INCREASE" else "diminuiu"
            return f"Nenhuma categoria de {noun}{_of(who)} {verb} {a} em relação {relative}."
        return NO_DATA
    kind = {"INCREASE": "os maiores aumentos", "DECREASE": "as maiores reduções"}.get(
        direction, "as variações"
    )
    contraction = "nas" if is_income else "nos"
    if who is None:
        target = f"{contraction} {'suas' if is_income else 'seus'} {noun}"
    else:
        target = f"{contraction} {noun}{_of(who)}"
    nothing = "receita" if is_income else "gasto"
    rows = []
    for c in changes:
        if c.previous == 0:
            detail = f"{format_brl(c.current)}; não houve {nothing} no período anterior"
        elif c.change == 0:
            detail = f"{format_brl(c.current)}, sem variação"
        else:
            detail = f"{_signed_brl(c.change)} ({_pct(c.percent, signed=True)})"
        rows.append(f"{c.category_name}: {detail}")
    return _lines(f"{_cap(a)}, {kind} {target} em relação {relative} foram:", rows)
