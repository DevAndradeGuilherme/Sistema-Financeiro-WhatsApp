from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower


class CategoryKind(models.TextChoices):
    EXPENSE = "EXPENSE", "Despesa"
    INCOME = "INCOME", "Receita"
    BOTH = "BOTH", "Despesa e receita"


def normalize_aliases(aliases: list[str]) -> list[str]:
    """Lowercase, strip, drop empties and duplicates (keeping order)."""
    result: list[str] = []
    for alias in aliases or []:
        value = " ".join(str(alias).split()).lower()
        if value and value not in result:
            result.append(value)
    return result


class CatalogEntry(models.Model):
    """Fields shared by categories and subcategories (abstract)."""

    name = models.CharField("nome", max_length=80)
    slug = models.SlugField("slug", max_length=80, help_text="Chave estável. Não altere após uso.")
    description = models.TextField("descrição", blank=True)
    aliases = ArrayField(
        models.CharField(max_length=60),
        verbose_name="aliases",
        default=list,
        blank=True,
        help_text="Termos que auxiliam a classificação (separados por vírgula).",
    )
    is_active = models.BooleanField("ativa", default=True)
    sort_order = models.PositiveSmallIntegerField("ordem", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        self.name = " ".join(self.name.split())
        self.aliases = normalize_aliases(self.aliases)


class Category(CatalogEntry):
    kind = models.CharField("tipo", max_length=10, choices=CategoryKind.choices)
    is_fallback = models.BooleanField(
        "fallback",
        default=False,
        help_text="Usada quando a mensagem não se encaixa em outra categoria. "
        "Exatamente uma por tipo (despesa e receita); precisa estar ativa.",
    )

    class Meta:
        verbose_name = "categoria"
        verbose_name_plural = "categorias"
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["slug"], name="category_slug_unique"),
            models.UniqueConstraint(
                Lower("name"),
                name="category_name_ci_unique",
                violation_error_message="Já existe uma categoria com esse nome.",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=CategoryKind.values),
                name="category_kind_valid",
            ),
            models.UniqueConstraint(
                fields=["kind"],
                condition=models.Q(is_fallback=True),
                name="category_single_fallback_per_kind",
                violation_error_message="Já existe uma categoria de fallback para esse tipo.",
            ),
            models.CheckConstraint(
                condition=models.Q(is_fallback=False)
                | (models.Q(is_active=True) & ~models.Q(kind=CategoryKind.BOTH)),
                name="category_fallback_active_and_typed",
                violation_error_message="Fallback deve estar ativa e ser de despesa ou receita.",
            ),
        ]

    def accepts(self, transaction_type: str) -> bool:
        return self.kind == CategoryKind.BOTH or self.kind == transaction_type

    def clean(self) -> None:
        super().clean()
        # Changing the kind must not make existing transactions inconsistent.
        if (
            self.pk
            and self.kind != CategoryKind.BOTH
            and self.transactions.exclude(type=self.kind).exists()
        ):
            raise ValidationError({"kind": "Existem transações de outro tipo nesta categoria."})


class Subcategory(CatalogEntry):
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="subcategories",
        verbose_name="categoria",
    )

    class Meta:
        verbose_name = "subcategoria"
        verbose_name_plural = "subcategorias"
        ordering = ["category__sort_order", "sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "slug"], name="subcategory_category_slug_unique"
            ),
            models.UniqueConstraint(
                "category",
                Lower("name"),
                name="subcategory_category_name_ci_unique",
                violation_error_message="Já existe uma subcategoria com esse nome nesta categoria.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category.name} › {self.name}"

    def clean(self) -> None:
        super().clean()
        # Moving a used subcategory to another category would break existing transactions.
        if self.pk:
            original_category_id = (
                Subcategory.objects.filter(pk=self.pk).values_list("category_id", flat=True).first()
            )
            if original_category_id != self.category_id and self.transactions.exists():
                raise ValidationError(
                    {"category": "Subcategoria em uso não pode mudar de categoria."}
                )
