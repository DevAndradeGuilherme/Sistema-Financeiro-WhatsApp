"""Every text sent to the user, built locally (never by the model). Short, pt-BR."""

from datetime import date
from decimal import Decimal

from common.formatting import describe_day, describe_period, format_brl

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
    *, is_income: bool, value: Decimal, scope: str | None, start: date, end: date, today: date
) -> str:
    verb = "recebeu" if is_income else "gastou"
    preposition = "em" if is_income else "com"
    scope_text = f" {preposition} {scope}" if scope else ""
    return f"Você {verb} {format_brl(value)}{scope_text} {describe_period(start, end, today)}."


def balance(*, income: Decimal, expense: Decimal, start: date, end: date, today: date) -> str:
    period = describe_period(start, end, today)
    return (
        f"Seu saldo {period} é {format_brl(income - expense)} "
        f"(entradas {format_brl(income)}; gastos {format_brl(expense)})."
    )
