"""Runtime instructions. Kept short on purpose: every token is paid on every message."""

from .contract import CatalogCategory, InterpretationContext

PROMPT_VERSION = "2026-10-07.4"

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_INSTRUCTIONS = """\
Extract a structured interpretation of ONE WhatsApp message (Brazilian Portuguese) for a \
personal finance app. Output only the JSON object.
Today: {today} ({weekday}). Timezone: {timezone}.

intent:
- CREATE_TRANSACTION: the user reports money spent (EXPENSE) or received (INCOME).
- QUERY_TRANSACTIONS: asks about money. query_kind: TOTAL (sum; set transaction_type); \
BALANCE; TOP (largest single items; "maior gasto/entrada" = limit 1 + transaction_type); \
BREAKDOWN (per category, "onde mais gastei"); LIST ("últimos gastos"); COMPARE (period A \
start/end vs B compare_start/compare_end); RANKING (per person, "quem gastou mais"); \
PERIOD_SUMMARY ("resumo", "como foi meu mês", "% da renda gasta"); MONTHLY_TREND (month by \
month or monthly average; months if stated; trend_metric EXPENSE/INCOME/BALANCE); \
CATEGORY_TREND (per-category change vs previous period, periods as COMPARE; \
trend_direction INCREASE "aumentaram"/DECREASE "gastei menos"/ALL).
- UPDATE_TRANSACTION: correct a transaction. reference = how it is identified (last=true \
for "último" or no criteria); updates = only the new values.
- UNDO_TRANSACTION: "desfazer", "cancela o último".
- HELP: how to use the bot. UNKNOWN: anything else.

scope (whose money; you only classify, the app decides permissions): SELF (default, \
"meu"); FAMILY ("minha família", "a gente", "gastamos", RANKING); FAMILY_MEMBER (one named \
person; target_member_name as written, e.g. "Maria"). Also for any intent naming another \
person ("registre 50 para a Maria").

Rules:
- amount: decimal string with dot, no currency ("R$ 10,50" -> "10.50", "dez reais" -> \
"10.00"). null if no amount is stated. Never guess.
- category_id / subcategory_id: only ids listed below, compatible with transaction_type; \
null when nothing fits clearly. Never invent ids. Also set them in queries that name a \
category or item ("de transporte" -> category; "com gasolina" -> subcategory).
- transaction_date: YYYY-MM-DD only if a date is mentioned ("ontem", "sexta", "dia 5"); a \
weekday means its most recent occurrence up to today; "dia N" means this month if N <= \
today's day, else last month. Otherwise null.
- start_date / end_date (queries): inclusive YYYY-MM-DD. "esse mês" = first day of this \
month to today; "hoje" = today; "ontem" = yesterday. null if no period is mentioned.
- description: short phrase from the message (e.g. "gasolina"), max 60 chars, or null.
- limit: number of items only if stated ("5 maiores"), else null.
- follow_up: true only if the message depends on the previous query ("e mês passado?", \
"e transporte?"); then return the complete query: previous query + what changed. If no \
previous query is given, still use QUERY_TRANSACTIONS with follow_up=true.
- Fields that do not apply to the intent (reference/updates unless UPDATE): null.

Catalog:
{catalog}"""


def render_catalog(catalog: tuple[CatalogCategory, ...]) -> str:
    lines = []
    for category in catalog:
        aliases = f" ({', '.join(category.aliases)})" if category.aliases else ""
        lines.append(f"category {category.id}: {category.name} [{category.kind}]{aliases}")
        for sub in category.subcategories:
            sub_aliases = f" ({', '.join(sub.aliases)})" if sub.aliases else ""
            lines.append(f"  subcategory {sub.id}: {sub.name}{sub_aliases}")
    return "\n".join(lines) or "(empty)"


def build_instructions(context: InterpretationContext) -> str:
    return _INSTRUCTIONS.format(
        today=context.reference_date.isoformat(),
        weekday=_WEEKDAYS[context.reference_date.weekday()],
        timezone=context.timezone,
        catalog=render_catalog(context.catalog),
    )


RECEIPT_PROMPT_VERSION = "2026-10-07.r1"

_RECEIPT_INSTRUCTIONS = """\
Read ONE image sent to a personal finance app (Brazil) and extract a structured proposal. \
Output only the JSON object. You only read; the app validates and decides.
Today: {today}. Timezone: {timezone}.

- is_financial_document: true only for a receipt, payment proof (Pix/transfer/card/boleto), \
fiscal coupon or invoice showing a paid/received amount. Selfies, landscapes, memes, \
screenshots of chats or anything else: false, document_type OTHER, other fields null/UNKNOWN.
- transaction_type: EXPENSE if the sender paid (compra, pagamento, "Pix enviado", \
"transferência enviada"); INCOME if they received ("Pix recebido", "você recebeu"). If the \
direction is not clearly shown: UNKNOWN. Never guess.
- amount: the FINAL TOTAL actually paid/received, as a decimal string with dot ("47.90"). \
Prefer "TOTAL"/"VALOR PAGO"/"valor da transferência" over subtotal; discounts already \
applied; ignore troco/change, tax lines, installment counts. Never use CPF, CNPJ, Pix key, \
account, authentication or transaction numbers as amount. null if not clearly legible.
- currency: BRL for R$/reais; OTHER if another currency is shown; UNKNOWN if none is shown.
- transaction_date: YYYY-MM-DD of the payment if printed; null if not legible. Never today \
unless printed.
- description: short generic label of what was paid ("posto de combustível", "mercado", \
"farmácia"), max 60 chars. Never include names of people, CPF/CNPJ, Pix keys, bank data or \
addresses. null if unclear.
- category_id / subcategory_id: only ids listed below, compatible with transaction_type; \
null when nothing fits clearly.

Catalog:
{catalog}"""


def build_receipt_instructions(context: InterpretationContext) -> str:
    return _RECEIPT_INSTRUCTIONS.format(
        today=context.reference_date.isoformat(),
        timezone=context.timezone,
        catalog=render_catalog(context.catalog),
    )
