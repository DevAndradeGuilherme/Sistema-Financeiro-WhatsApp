"""Runtime instructions. Kept short on purpose: every token is paid on every message."""

from .contract import CatalogCategory, InterpretationContext

PROMPT_VERSION = "2026-10-07.2"

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_INSTRUCTIONS = """\
Extract a structured interpretation of ONE WhatsApp message (Brazilian Portuguese) for a \
personal finance app. Output only the JSON object.
Today: {today} ({weekday}). Timezone: {timezone}.

intent:
- CREATE_TRANSACTION: the user reports money spent (EXPENSE) or received (INCOME).
- QUERY_TRANSACTIONS: the user asks about their OWN money. query_kind: TOTAL = sum of \
expenses or income (set transaction_type); BALANCE = income minus expenses; TOP = largest \
single expenses ("maiores gastos"); BREAKDOWN = totals per category ("onde mais gastei", \
"por categoria"); LIST = list transactions ("últimos gastos"); \
COMPARE = period A (start/end) vs period B (compare_start/compare_end).
- UPDATE_TRANSACTION: correct an existing transaction. reference = how the user identifies \
it (last=true for "último"/"muda para 15" without criteria); updates = only the new values.
- UNDO_TRANSACTION: undo/cancel the last action ("desfazer", "cancela o último").
- HELP: asks how to use the bot.
- UNKNOWN: anything else, including anything about other people's money or transactions.

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
