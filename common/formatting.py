"""pt-BR presentation helpers (no business rules, no locale dependency)."""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .money import TWO_PLACES

MONTHS = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)


def format_brl(value: Decimal) -> str:
    """Decimal('1234.5') → 'R$ 1.234,50'; negative → '-R$ 50,00'."""
    if not isinstance(value, Decimal):
        raise TypeError("format_brl expects Decimal.")
    quantized = value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    integer, cents = f"{abs(quantized):.2f}".split(".")
    groups = []
    while integer:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    return f"{sign}R$ {'.'.join(groups)},{cents}"


def format_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def describe_day(value: date, today: date) -> str:
    if value == today:
        return "hoje"
    if value == today - timedelta(days=1):
        return "ontem"
    return f"em {format_date(value)}"


def describe_period(start: date, end: date, today: date) -> str:
    """Short pt-BR phrase: 'hoje', 'ontem', 'em outubro', 'em outubro de 2025', 'de x a y'."""
    if start == end:
        return describe_day(start, today)
    if start.day == 1 and start.year == end.year and start.month == end.month:
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_end = next_month - timedelta(days=1)
        if end in (month_end, today):
            month = MONTHS[start.month - 1]
            return f"em {month}" if start.year == today.year else f"em {month} de {start.year}"
    return f"de {format_date(start)} a {format_date(end)}"
