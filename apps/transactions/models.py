import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class TransactionType(models.TextChoices):
    EXPENSE = "EXPENSE", "Despesa"
    INCOME = "INCOME", "Receita"


class TransactionSource(models.TextChoices):
    TEXT = "TEXT", "Texto"
    AUDIO = "AUDIO", "Áudio"
    IMAGE = "IMAGE", "Imagem"
    MANUAL = "MANUAL", "Manual"
    ADMIN = "ADMIN", "Admin"


class TransactionQuerySet(models.QuerySet):
    def active(self) -> "TransactionQuerySet":
        return self.filter(deleted_at__isnull=True)

    def deleted(self) -> "TransactionQuerySet":
        return self.filter(deleted_at__isnull=False)


class Transaction(models.Model):
    """An expense or income owned by ``user``.

    ``amount`` is always positive; the direction comes from ``type``. ``family`` records the
    owner's family at creation time (set by the service, never chosen freely).
    Domain rules that need context (active catalog, dates, limits, family) live in
    ``services.create_transaction``; relational consistency is also checked in ``clean``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="transactions",
        verbose_name="usuário",
    )
    family = models.ForeignKey(
        "families.Family",
        on_delete=models.PROTECT,
        related_name="transactions",
        null=True,
        blank=True,
        verbose_name="família",
    )
    type = models.CharField("tipo", max_length=10, choices=TransactionType.choices)
    amount = models.DecimalField("valor", max_digits=14, decimal_places=2)
    category = models.ForeignKey(
        "categories.Category",
        on_delete=models.PROTECT,
        related_name="transactions",
        verbose_name="categoria",
    )
    subcategory = models.ForeignKey(
        "categories.Subcategory",
        on_delete=models.PROTECT,
        related_name="transactions",
        null=True,
        blank=True,
        verbose_name="subcategoria",
    )
    description = models.CharField("descrição", max_length=255, blank=True)
    transaction_date = models.DateField("data da transação")
    source = models.CharField("origem", max_length=10, choices=TransactionSource.choices)
    idempotency_key = models.CharField(
        "chave de idempotência", max_length=200, null=True, blank=True, unique=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField("excluída em", null=True, blank=True)

    objects = TransactionQuerySet.as_manager()

    class Meta:
        verbose_name = "transação"
        verbose_name_plural = "transações"
        ordering = ["-transaction_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="transaction_amount_positive",
                violation_error_message="O valor deve ser maior que zero.",
            ),
            models.CheckConstraint(
                condition=models.Q(type__in=TransactionType.values),
                name="transaction_type_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=TransactionSource.values),
                name="transaction_source_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(idempotency_key=""),
                name="transaction_idempotency_key_not_empty",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "transaction_date"], name="transaction_user_date_idx"),
            models.Index(
                fields=["user", "category", "transaction_date"],
                name="transaction_user_cat_date_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_type_display()} R$ {self.amount} — {self.transaction_date}"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.category_id and self.type and not self.category.accepts(self.type):
            errors["category"] = "Categoria incompatível com o tipo da transação."
        if self.subcategory_id and self.subcategory.category_id != self.category_id:
            errors["subcategory"] = "Subcategoria não pertence à categoria informada."
        if errors:
            raise ValidationError(errors)

    def mark_deleted(self) -> None:
        self.deleted_at = timezone.now()
