"""Reads the structured CREATE confirmation so tests compare its fields, not its layout."""

from datetime import date

from common.formatting import format_date


def card(text: str) -> dict[str, str]:
    """The CREATE card as {'title': '✅ Gasto registrado', 'Valor': 'R$ 10,00', ...}.

    The title is the first paragraph; each field line is '<emoji> <Label>: <value>' and the emoji
    is dropped. A missing subcategory simply has no 'Subcategoria' key. Paragraphs after the
    fields (e.g. the receipt note) are ignored.
    """
    title, _, body = text.partition("\n\n")
    fields = {"title": title}
    for line in body.partition("\n\n")[0].splitlines():
        label, _, value = line.split(" ", 1)[1].partition(": ")
        fields[label] = value
    return fields


def expense(amount: str, category: str, day: date, subcategory: str | None = None) -> dict:
    return _fields("✅ Gasto registrado", amount, category, subcategory, day)


def income(amount: str, category: str, day: date, subcategory: str | None = None) -> dict:
    return _fields("✅ Receita registrada", amount, category, subcategory, day)


def _fields(title, amount, category, subcategory, day) -> dict[str, str]:
    fields = {"title": title, "Valor": f"R$ {amount}", "Categoria": category}
    if subcategory:
        fields["Subcategoria"] = subcategory
    fields["Data"] = format_date(day)
    return fields


def rows(text: str) -> list[str]:
    """The lines of a reply's list paragraph (the one right after the intro)."""
    return text.split("\n\n")[1].splitlines()
