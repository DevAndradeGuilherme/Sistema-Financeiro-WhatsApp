from django.conf import settings
from django.db import models


class Family(models.Model):
    name = models.CharField("nome", max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "família"
        verbose_name_plural = "famílias"

    def __str__(self) -> str:
        return self.name


class FamilyMember(models.Model):
    """Links a user to a family. A user belongs to at most one family; a family has one ADMIN."""

    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Administrador"
        MEMBER = "MEMBER", "Membro"

    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name="memberships")
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="family_membership",
        verbose_name="usuário",
    )
    role = models.CharField("papel", max_length=10, choices=Role.choices, default=Role.MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "membro da família"
        verbose_name_plural = "membros da família"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=["ADMIN", "MEMBER"]),
                name="family_member_role_valid",
            ),
            models.UniqueConstraint(
                fields=["family"],
                condition=models.Q(role="ADMIN"),
                name="family_single_admin",
                violation_error_message="A família já possui um administrador.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} ({self.get_role_display()}) — {self.family}"

    @property
    def is_admin(self) -> bool:
        return self.role == self.Role.ADMIN
