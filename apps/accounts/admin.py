from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.urls import reverse
from django.utils.html import format_html

from .forms import UserChangeForm, UserCreationForm
from .models import User

PRIVILEGE_FIELDS = ("is_staff", "is_superuser", "groups", "user_permissions")


class FamilyFilter(admin.SimpleListFilter):
    title = "família"
    parameter_name = "has_family"

    def lookups(self, request, model_admin):
        return (("yes", "Com família"), ("no", "Sem família"))

    def queryset(self, request, queryset):
        if self.value() in {"yes", "no"}:
            return queryset.filter(family_membership__isnull=self.value() == "no")
        return queryset


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    list_display = (
        "full_name",
        "username",
        "phone_number",
        "is_active",
        "is_staff",
        "is_superuser",
        "current_family",
        "family_role",
    )
    list_filter = ("is_active", "is_staff", "is_superuser", FamilyFilter)
    search_fields = ("username", "phone_number", "first_name", "last_name", "email")
    fieldsets = (
        (None, {"fields": ("username", "password_access")}),
        ("Dados pessoais", {"fields": ("first_name", "last_name", "email", "phone_number")}),
        (
            "Permissões",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Datas", {"fields": ("last_login", "date_joined")}),
        ("Família atual", {"fields": ("current_family", "family_role")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "phone_number",
                    "first_name",
                    "last_name",
                    "email",
                    "usable_password",
                    "password1",
                    "password2",
                ),
            },
        ),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("family_membership__family")

    @admin.display(description="nome", ordering="first_name")
    def full_name(self, obj):
        return obj.get_full_name() or obj.username

    @admin.display(description="família atual")
    def current_family(self, obj):
        membership = getattr(obj, "family_membership", None)
        return membership.family if membership else "—"

    @admin.display(description="papel familiar")
    def family_role(self, obj):
        membership = getattr(obj, "family_membership", None)
        return membership.role if membership else "—"

    @admin.display(description="autenticação por senha")
    def password_access(self, obj):
        status = "Habilitada" if obj.has_usable_password() else "Desabilitada (WhatsApp)"
        return format_html(
            '{} — <a href="{}">Configurar senha</a>',
            status,
            reverse("admin:auth_user_password_change", args=[obj.pk]),
        )

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        readonly += [
            "last_login",
            "date_joined",
            "password_access",
            "current_family",
            "family_role",
        ]
        # Only superusers may grant or change privileges (prevents staff self-escalation).
        if not request.user.is_superuser:
            readonly += list(PRIVILEGE_FIELDS)
        return readonly

    def has_change_permission(self, request, obj=None):
        # Also prevent account takeover of a different, more privileged staff user.
        if (
            obj is not None
            and not request.user.is_superuser
            and (obj.is_superuser or (obj.is_staff and obj.pk != request.user.pk))
        ):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        # CASCADE would remove the current ADMIN membership; SET_NULL would lose message identity.
        # Keep operational history and family invariants: deactivate all accounts instead.
        return False
