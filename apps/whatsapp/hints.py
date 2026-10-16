"""Deterministic reading of the message text (defense in depth for mutations).

The model's proposal is untrusted and may *drop* or *contradict* criteria ("desfaça a entrada
de 50" → UNDO without reference; "apague a entrada de 77 da Maria" → no member). These hints
let Django notice what the text explicitly states, so a mutation never degrades to a broader
search or to "the latest transaction", and a correction never becomes a creation. They only
restrict: a hint can demand clarification or refuse, never select a transaction, add a filter
the text does not state, or widen access. This is not a parallel NLP: only explicit, easily
recognizable pt-BR tokens are read, on lowercase accent-free text; anything else is left to
the model and to the 0/1/N rule.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

_DAY = r"(?:0?[1-9]|[12]\d|3[01])"  # day of month written before "de <mês>"
_MONTHS = "janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro"
_INCOME = re.compile(r"\b(?:entradas?|receitas?|recebi|recebido|recebimentos?|ganhei)\b")
_EXPENSE = re.compile(r"\b(?:gastos?|despesas?|gastei|paguei|compras?|comprei)\b")
# Explicitly several: "todas as entradas", "os gastos de ontem", "esses lançamentos".
_BULK = re.compile(
    r"\b(?:todas|todos|tudo|entradas|receitas|gastos|despesas|lancamentos|transacoes|registros)\b"
)
# Explicitly relative to the sender's latest registration ("o último", "que acabei de...").
# Nothing else means "the latest": "desfazer" alone identifies nothing.
_LAST = re.compile(r"\b(?:ultim[oa]|acabei|mais recente|agora ha pouco|agorinha)\b")
# "desfaz a última alteração" = undo the last action (stack), not "delete a transaction".
_ACTION = re.compile(r"\b(?:alteracao|edicao|mudanca|acao|correcao)\b")
_DATE_TOKENS = (
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    r"\bdia\s+\d{1,2}\b",
    rf"\b{_DAY}\s+de\s+(?:{_MONTHS})(?:\s+de\s+\d{{4}})?\b",
    rf"\b(?:{_MONTHS})\s+de\s+\d{{4}}\b",
)
_DAY_MONTH = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_RELATIVE_DAYS = {"hoje": 0, "ontem": 1, "anteontem": 2}
_DATE_WORDS = re.compile(
    rf"\b(?:hoje|ontem|anteontem|semana|mes|ano|{_MONTHS}|"
    r"segunda|terca|quarta|quinta|sexta|sabado|domingo)\b"
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_COPULA = r"(?:foi|foram|era|eram|e|sao)"
_AMOUNT = r"(?:r\$\s*)?\d[\d.,]*(?:\s*reais)?"
_CORRECTION = re.compile(
    r"^\W*(?:errado|errei|erro|corrig\w*|corrij\w*|era|eram|me enganei)\b"
    r"|\b(?:errado|errei|na verdade|na real|quis dizer|me enganei|digitei errado|corrigindo"
    rf"|valor (?:certo|correto)|o certo {_COPULA})\b"
    r"|\bnao foi\b.*\bfoi\b"
    # "não, foram 80"
    rf"|\bnao\W+{_COPULA}\s+{_AMOUNT}"
    # "foi 80, não 100"
    rf"|\b{_COPULA}\s+{_AMOUNT}\W+(?:e\s+)?nao\b"
    # "desculpa, foram 80" (an apology alone, "desculpa, gastei 50", is not a correction)
    r"|^\W*desculp\w*\b.*\b(?:foi|foram|era|eram)\b"
)
# Words that carry no new transaction (amount-only/elliptic messages: "foram 80", "não, 80").
_FILLER = frozenset(
    {
        "r",
        "rs",
        "reais",
        "real",
        "conto",
        "contos",
        "foi",
        "foram",
        "era",
        "eram",
        "e",
        "sao",
        "na",
        "no",
        "verdade",
        "nao",
        "sim",
        "ok",
        "desculpa",
        "desculpe",
        "opa",
        "ops",
        "ah",
        "so",
        "somente",
        "apenas",
        "valor",
        "o",
        "a",
        "os",
        "as",
        "de",
        "do",
        "da",
        "certo",
        "correto",
        "isso",
        "mas",
        "pois",
        "entao",
        "corrigindo",
        "quis",
        "dizer",
        "errado",
        "errei",
        "me",
        "enganei",
    }
)
_ELLIPTIC_CORRECTION = re.compile(r"\b(?:foi|foram|era|eram|nao|desculp\w*)\b")
_RECORD_NOUNS = (
    r"(?:lancamentos?|registros?|transac(?:ao|oes)|gastos?|despesas?|entradas?|receitas?"
    r"|compras?|pagamentos?)"
)
# Explicit order to remove/edit an existing registration: such a text is never a new
# transaction, whatever the model proposed. Only unequivocal verbs; "cancelar", "tirar",
# "retirar" and "estornar" only applied to a registration ("tira esse gasto de 30"), never alone
# ("cancelei a assinatura", "retirei 200 no caixa", "tira-gosto", "tirar uma tatuagem").
_REMOVE = re.compile(
    r"\b(?:remov(?:e|a|er|am)|apag(?:a|ue|ar|uem)|exclu(?:i|a|ir|am)|delet(?:a|e|ar|em)"
    r"|desfa(?:z|ca|zer|zam|ze))\b"
    r"|\b(?:cancel|tir|retir|estorn)(?:a|e|ar|em)\s+"
    r"(?:(?:[oa]s?|esse|essa|este|esta|meu|minha|ultim[oa]s?)\s+)*"
    rf"{_RECORD_NOUNS}\b"
)
# "troca"/"muda" only with what changes ("muda o valor"): "troca de óleo" is a purchase.
_EDIT = re.compile(
    r"\b(?:edit(?:a|e|ar)|alter(?:a|e|ar))\b"
    r"|\b(?:mud(?:a|e|ar)|troc(?:a|ar)|troque)\s+(?:(?:[oa]|esse|essa)\s+)?"
    r"(?:valor|data|categoria|descricao)\b"
)
# A day attached to a registration ("o gasto de ontem", "a entrada de 50 do dia 07/10", "o de
# ontem") identifies the transaction: in an UPDATE it must be a criterion, never only the new
# date. A copula or "para" in between ("o gasto era de ontem") makes it a new value instead.
_WEEKDAYS = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
_REFERENCE_DATE = re.compile(
    rf"(?:\b[oa]s?\s+|\b{_RECORD_NOUNS}\s+"
    r"(?:(?!(?:era|eram|foi|foram|e|sao|para|pra|pro|data)\b)[\w$]+\s+){0,3}?)"
    r"(?P<prep>d[eoa]s?|em)\s+"
    r"(?P<date>hoje|ontem|anteontem|dia\s+\d{1,2}(?:/\d{1,2}(?:/\d{2,4})?)?"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    rf"|(?:{_DAY}\s+de\s+)?(?:{_MONTHS})(?:\s+de\s+\d{{4}})?"
    rf"|(?:{'|'.join(_WEEKDAYS)})(?:[\s-]feira)?)\b"
)


@dataclass(frozen=True)
class TextHints:
    type: str | None = None  # INCOME / EXPENSE when the text names only one of them
    amount: bool = False  # a number that is not part of a date
    amounts: tuple = ()  # every such number, parsed (Decimal) or None when unreadable
    date: bool = False  # a day or period ("07/10", "ontem", "de outubro")
    day_months: tuple = ()  # explicit dd/mm[/yyyy]: (day, month, year | None)
    days_ago: tuple = ()  # "hoje"/"ontem"/"anteontem": 0 / 1 / 2
    bulk: bool = False  # several transactions ("todas", plural nouns)
    last: bool = False  # explicitly relative ("o último", "o que acabei de registrar")
    action: bool = False  # "a última alteração" (the undo stack)
    correction: bool = False  # "errado", "na verdade", "desculpa, foram 80", "foi 80, não 100"
    elliptic: bool = False  # only an amount and filler words: no new transaction is described
    mutation: str | None = None  # "REMOVE" / "EDIT": explicit order to change a registration
    # Date fragments that identify the transaction ("de ontem", "do dia 5"); see ``temporal``.
    reference_dates: tuple = ()


def normalize(text: str | None) -> str:
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(value.lower().split())


def read(text: str | None) -> TextHints:
    normalized = normalize(text)
    income = bool(_INCOME.search(normalized))
    expense = bool(_EXPENSE.search(normalized))
    without_dates = normalized
    has_date = False
    for pattern in _DATE_TOKENS:
        without_dates, found = re.subn(pattern, " ", without_dates)
        has_date = has_date or found > 0
    named = {"INCOME"} if income else set()
    named |= {"EXPENSE"} if expense else set()
    amounts = tuple(parse_amount(token) for token in _NUMBER.findall(without_dates))
    leftover = set(re.findall(r"[a-z]+", _NUMBER.sub(" ", without_dates))) - _FILLER
    elliptic = bool(amounts) and not has_date and not leftover
    return TextHints(
        type=named.pop() if len(named) == 1 else None,
        amount=bool(amounts),
        amounts=amounts,
        date=has_date or bool(_DATE_WORDS.search(normalized)),
        day_months=tuple((int(d), int(m), _year(y)) for d, m, y in _DAY_MONTH.findall(normalized)),
        days_ago=tuple(
            offset
            for word, offset in _RELATIVE_DAYS.items()
            if re.search(rf"\b{word}\b", normalized)
        ),
        bulk=bool(_BULK.search(normalized)),
        last=bool(_LAST.search(normalized)),
        action=bool(_ACTION.search(normalized)),
        correction=bool(_CORRECTION.search(normalized))
        or (elliptic and bool(_ELLIPTIC_CORRECTION.search(normalized))),
        elliptic=elliptic,
        mutation="REMOVE"
        if _REMOVE.search(normalized)
        else "EDIT"
        if _EDIT.search(normalized)
        else None,
        reference_dates=tuple(
            f"{m.group('prep')} {m.group('date')}" for m in _REFERENCE_DATE.finditer(normalized)
        ),
    )


# --- Temporal expressions -----------------------------------------------------------------
#
# Every date/period the text states becomes a closed span of ``transaction_date`` values,
# resolved with the project's date rules (docs/DOMAIN.md §4), so a proposal can be compared
# with *what* the text says, not only with *whether* it says a date. Anything that looks like
# a date but is not one of these forms (or is not a valid date) is ``unresolved``: the caller
# asks instead of guessing.


@dataclass(frozen=True)
class Temporal:
    spans: tuple = ()  # (start, end) per resolved expression (a day is (d, d))
    unresolved: bool = False


def temporal(text: str | None, today: date) -> Temporal:
    remaining = normalize(text)
    spans: list[tuple[date, date]] = []
    unresolved = False
    for pattern, resolve in _TEMPORAL_FORMS:

        def consume(match, resolve=resolve):
            nonlocal unresolved
            span = resolve(match, today)
            if span is None:
                unresolved = True
            else:
                spans.append(span)
            return " "

        remaining = pattern.sub(consume, remaining)
    if _DATE_WORDS.search(remaining) or _DATE_LEFTOVER.search(remaining):
        unresolved = True
    return Temporal(spans=tuple(spans), unresolved=unresolved)


def _month_number(name: str) -> int:
    return _MONTHS.split("|").index(name) + 1


def _month_end(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _day(span_day: date | None) -> tuple[date, date] | None:
    return (span_day, span_day) if span_day else None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _latest(month: int, day: int, today: date) -> date | None:
    """dd/mm without year: its most recent occurrence up to today (no future dates)."""
    for year in (today.year, today.year - 1):
        candidate = _safe_date(year, month, day)
        if candidate is not None and candidate <= today:
            return candidate
    return None


def _numeric_date(m, today: date):
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if year is None:
        return _day(_latest(month, day, today))
    if len(year) not in (2, 4):
        return None
    return _day(_safe_date(_year(year), month, day))


def _day_of_month_name(m, today: date):
    day, month, year = int(m.group(1)), _month_number(m.group(2)), m.group(3)
    if year:
        return _day(_safe_date(int(year), month, day))
    return _day(_latest(month, day, today))


def _month_name(m, today: date):
    month = _month_number(m.group(1))
    # Without a year: the most recent such month that has started.
    latest = today.year if month <= today.month else today.year - 1
    start = date(int(m.group(2)) if m.group(2) else latest, month, 1)
    return start, _month_end(start)


def _day_n(m, today: date):
    """'dia N': this month if N <= today's day, else last month (docs/DOMAIN.md §4)."""
    n = int(m.group(1))
    if n <= today.day:
        return _day(_safe_date(today.year, today.month, n))
    previous = today.replace(day=1) - timedelta(days=1)
    return _day(_safe_date(previous.year, previous.month, n))


def _relative_day(m, today: date):
    return _day(today - timedelta(days=_RELATIVE_DAYS[m.group(1)]))


_UNQUALIFYING = {"na", "de", "da", "do", "desde", "em", "no"}


def _weekday(m, today: date):
    """Unqualified weekday: its most recent occurrence up to today. "sexta passada", "próxima
    segunda", "desta quarta" and a bare "segunda" (ordinal: "a segunda entrada") → unresolved."""
    before = (m.group("before") or "").strip()
    if m.group("after") or before in {"ultima", "ultimo", "proxima", "proximo"} | _DEMONSTRATIVE:
        return None
    if m.group("wd") == "segunda" and not m.group("feira") and before not in _UNQUALIFYING:
        return None
    back = (today.weekday() - _WEEKDAYS.index(m.group("wd"))) % 7
    return _day(today - timedelta(days=back))


def _this_month(m, today: date):
    return today.replace(day=1), _month_end(today)


def _last_month(m, today: date):
    end = today.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def _week_bounds(today: date) -> tuple[date, date]:
    """'Esta semana' under either convention (Monday–Sunday or Sunday–Saturday): the span
    covers both, so a proposal is never refused for the convention, only for another week."""
    monday = today - timedelta(days=today.weekday())
    sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    return min(monday, sunday), max(monday, sunday) + timedelta(days=6)


def _this_week(m, today: date):
    return _week_bounds(today)


def _last_week(m, today: date):
    start, end = _week_bounds(today)
    return start - timedelta(days=7), end - timedelta(days=7)


def _this_year(m, today: date):
    return date(today.year, 1, 1), date(today.year, 12, 31)


def _last_year(m, today: date):
    return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)


_DEMONSTRATIVE = {"esse", "este", "essa", "esta", "nesse", "neste", "nessa", "nesta"}
_DEMONSTRATIVE |= {"desse", "deste", "dessa", "desta"}
_THIS = r"\b(?:n|d)?(?:esse|este|essa|esta)\s+"
_TEMPORAL_FORMS = (
    (re.compile(r"(?:\bdia\s+)?\b(\d{1,2})/(\d{1,2})(?:/(\d+))?\b"), _numeric_date),
    (
        re.compile(rf"(?:\bdia\s+)?\b({_DAY})\s+de\s+({_MONTHS})(?:\s+de\s+(\d{{4}}))?\b"),
        _day_of_month_name,
    ),
    (re.compile(rf"\b({_MONTHS})(?:\s+de\s+(\d{{4}}))?\b"), _month_name),
    (re.compile(r"\bdia\s+(\d{1,2})\b"), _day_n),
    (re.compile(r"\b(hoje|ontem|anteontem)\b"), _relative_day),
    (re.compile(rf"{_THIS}mes\b|\bmes\s+atual\b"), _this_month),
    (re.compile(r"\bmes\s+(?:passado|anterior)\b"), _last_month),
    (re.compile(rf"{_THIS}semana\b|\bsemana\s+atual\b"), _this_week),
    (re.compile(r"\bsemana\s+(?:passada|anterior)\b"), _last_week),
    (re.compile(rf"{_THIS}ano\b|\bano\s+atual\b"), _this_year),
    (re.compile(r"\bano\s+(?:passado|anterior)\b"), _last_year),
    (
        re.compile(
            r"(?:\b(?P<before>[a-z]+)\s+)?\b(?P<wd>" + "|".join(_WEEKDAYS) + r")"
            r"(?P<feira>[\s-]feira)?"
            r"(?P<after>\s+(?:passad[ao]|retrasad[ao]|que\s+vem|proxim[ao]|anterior))?\b"
        ),
        _weekday,
    ),
)
# Date-like leftovers no form above resolved ("dia 32" is consumed as unresolved already).
_DATE_LEFTOVER = re.compile(r"\bdia\s+\d|\d/\d")


def parse_amount(token: str) -> Decimal | None:
    """pt-BR number as written ("50", "1.050,41", "10.50") → Decimal, or None if unclear."""
    if "," in token:
        token = token.replace(".", "").replace(",", ".")
    elif token.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", token):
        token = token.replace(".", "")
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def find_terms(text: str | None, terms: Iterable[str]) -> set[str]:
    """Normalized ``terms`` present in the text as whole words (longest first, no overlap)."""
    remaining = normalize(text)
    found = set()
    for term in sorted({t for t in terms if t}, key=len, reverse=True):
        pattern = rf"\b{re.escape(term)}\b"
        if re.search(pattern, remaining):
            found.add(term)
            remaining = re.sub(pattern, " ", remaining)
    return found


# Words that never describe *which* registration: grammar, the mutation request itself,
# record/type nouns and date words. What is left ("mercado", "gasolina") can identify one.
_STOPWORDS = (
    "para pra pro que com sem uma uns umas dos das nos nas num numa pelo pela pelos pelas"
    " ele ela esse essa este esta isso isto aquele aquela meu minha meus minhas seu sua"
    " ultimo ultima ultimos ultimas data categoria subcategoria descricao novo nova"
    " mais menos por favor dia mes ano semana hoje ontem anteontem"
)
_NOT_DESCRIPTIVE = frozenset(
    {
        *_FILLER,
        *_STOPWORDS.split(),
        *_MONTHS.split("|"),
        *_WEEKDAYS,
    }
)
_REQUEST_WORD = re.compile(
    r"(?:alter|edit|remov|apag|exclu|delet|desfa|corrig|corrij|registr)\w*"
    r"|(?:mud|troc|cancel|tir|retir|estorn|lanc|anot)(?:a|e|ar|em|am|ei|ou)"
)


def descriptive_words(text: str | None) -> set[str]:
    """Whole words of ``text`` (3+ letters) that can describe a registration."""
    return {
        word
        for word in re.findall(r"[a-z]+", normalize(text))
        if len(word) >= 3
        and word not in _NOT_DESCRIPTIVE
        and not _REQUEST_WORD.fullmatch(word)
        and not re.fullmatch(_RECORD_NOUNS, word)
        and not names_a_type(word)
    }


def names_a_type(term: str) -> bool:
    """True when ``term`` is also a type word ("compras"): never read as a catalog item."""
    return bool(_INCOME.search(term) or _EXPENSE.search(term))


def _year(value: str) -> int | None:
    if not value:
        return None
    year = int(value)
    return year + 2000 if year < 100 else year
