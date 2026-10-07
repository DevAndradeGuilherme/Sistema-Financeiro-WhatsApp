from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models

from common.phone import E164_PATTERN, InvalidPhoneNumber, normalize_phone


class User(AbstractUser):
    """Custom user. Used by Django Admin and identified on WhatsApp by ``phone_number``."""

    phone_number = models.CharField(
        "telefone (E.164)",
        max_length=16,
        unique=True,
        null=True,
        blank=True,
        help_text="Formato canônico internacional, ex.: +5531999998888.",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(phone_number__regex=E164_PATTERN),
                name="user_phone_number_e164",
                violation_error_message="Telefone deve estar no formato E.164.",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.phone_number in (None, ""):
            self.phone_number = None
            return
        try:
            self.phone_number = normalize_phone(self.phone_number)
        except InvalidPhoneNumber as exc:
            raise ValidationError({"phone_number": str(exc)}) from exc
