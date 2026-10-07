"""Initial category catalog.

Applied by ``python manage.py seed_categories``. Idempotent: only creates entries whose slug
does not exist yet and never overwrites changes made by the administrator in Django Admin.
"""

from dataclasses import dataclass, field

from django.db import transaction
from django.utils.text import slugify

from .models import Category, CategoryKind, Subcategory


@dataclass(frozen=True)
class SeedCategory:
    name: str
    kind: str
    aliases: tuple[str, ...] = ()
    subcategories: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)


EXPENSE = CategoryKind.EXPENSE
INCOME = CategoryKind.INCOME

INITIAL_CATALOG: tuple[SeedCategory, ...] = (
    SeedCategory("Alimentação", EXPENSE, ("comida", "mercado", "restaurante", "lanche")),
    SeedCategory(
        "Transporte",
        EXPENSE,
        ("carro", "locomoção"),
        (
            ("Combustível", ("gasolina", "etanol", "álcool", "diesel", "posto")),
            ("Uber", ("99", "táxi", "corrida")),
            ("Transporte público", ("ônibus", "metrô", "trem")),
            ("Pedágio", ()),
            ("Estacionamento", ()),
            ("Manutenção", ("oficina", "mecânico", "revisão")),
        ),
    ),
    SeedCategory("Moradia", EXPENSE, ("aluguel", "condomínio", "iptu")),
    SeedCategory("Saúde", EXPENSE, ("farmácia", "remédio", "médico", "consulta")),
    SeedCategory("Educação", EXPENSE, ("escola", "curso", "faculdade", "livro")),
    SeedCategory("Lazer", EXPENSE, ("cinema", "viagem", "passeio")),
    SeedCategory("Compras", EXPENSE, ("roupa", "loja")),
    SeedCategory("Assinaturas", EXPENSE, ("streaming", "netflix", "spotify")),
    SeedCategory("Contas", EXPENSE, ("luz", "água", "internet", "telefone", "energia")),
    SeedCategory("Investimentos", EXPENSE, ("aporte", "poupança")),
    SeedCategory("Outros gastos", EXPENSE),
    SeedCategory("Salário", INCOME, ("pagamento", "holerite")),
    SeedCategory("Outras receitas", INCOME, ("rendimento", "reembolso", "freela")),
)


@transaction.atomic
def seed_categories(catalog: tuple[SeedCategory, ...] = INITIAL_CATALOG) -> dict[str, int]:
    created = {"categories": 0, "subcategories": 0}
    for order, item in enumerate(catalog, start=1):
        category, was_created = Category.objects.get_or_create(
            slug=slugify(item.name),
            defaults={
                "name": item.name,
                "kind": item.kind,
                "aliases": list(item.aliases),
                "sort_order": order * 10,
            },
        )
        created["categories"] += int(was_created)
        for sub_order, (sub_name, sub_aliases) in enumerate(item.subcategories, start=1):
            _, sub_created = Subcategory.objects.get_or_create(
                category=category,
                slug=slugify(sub_name),
                defaults={
                    "name": sub_name,
                    "aliases": list(sub_aliases),
                    "sort_order": sub_order * 10,
                },
            )
            created["subcategories"] += int(sub_created)
    return created
