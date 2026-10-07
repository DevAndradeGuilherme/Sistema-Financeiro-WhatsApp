from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet

from .models import Family, FamilyMember

ADMIN = FamilyMember.Role.ADMIN
SWITCH_ADMIN_HINT = (
    "Para trocar o administrador, edite a família e altere os papéis na mesma operação."
)


class FamilyMemberInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        admins = [
            form
            for form in self.forms
            if form.cleaned_data
            and not form.cleaned_data.get("DELETE")
            and form.cleaned_data.get("role") == ADMIN
        ]
        if len(admins) > 1:
            raise ValidationError("A família deve ter exatamente um administrador.")
        has_members = any(
            form.cleaned_data and not form.cleaned_data.get("DELETE") for form in self.forms
        )
        if has_members and not admins:
            raise ValidationError("A família deve ter exatamente um administrador.")


class FamilyMemberInline(admin.TabularInline):
    model = FamilyMember
    formset = FamilyMemberInlineFormSet
    extra = 0
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at",)


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    list_display = ("name", "member_count", "created_at")
    search_fields = ("name",)
    readonly_fields = ("created_at", "updated_at")
    inlines = [FamilyMemberInline]

    @admin.display(description="membros")
    def member_count(self, obj: Family) -> int:
        return obj.memberships.count()

    def save_formset(self, request, form, formset, change):
        """Apply deletions and demotions before promotions.

        The database allows at most one ADMIN per family (partial unique index, which
        PostgreSQL checks per statement), so swapping the ADMIN in one form must demote first.
        """
        if formset.model is not FamilyMember:
            return super().save_formset(request, form, formset, change)
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        for obj in sorted(instances, key=lambda member: member.role == ADMIN):
            obj.save()
        formset.save_m2m()


class FamilyMemberForm(forms.ModelForm):
    class Meta:
        model = FamilyMember
        fields = ("family", "user", "role")

    def clean(self):
        cleaned = super().clean()
        current = self.instance
        if current.pk and current.role == ADMIN:
            moved = cleaned.get("family") is not None and cleaned["family"].pk != current.family_id
            if cleaned.get("role") != ADMIN or moved:
                raise ValidationError(SWITCH_ADMIN_HINT)
        return cleaned


@admin.register(FamilyMember)
class FamilyMemberAdmin(admin.ModelAdmin):
    """Standalone membership editing; it can never leave a family without its ADMIN."""

    form = FamilyMemberForm
    list_display = ("user", "family", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__phone_number", "family__name")
    autocomplete_fields = ("user", "family")
    readonly_fields = ("created_at",)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)  # bulk delete would bypass the ADMIN check
        return actions

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_admin and obj.family.memberships.exclude(pk=obj.pk).exists():
            return False
        return super().has_delete_permission(request, obj)
