from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet

from .models import Family, FamilyMember


class FamilyMemberInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        admins = [
            form
            for form in self.forms
            if form.cleaned_data
            and not form.cleaned_data.get("DELETE")
            and form.cleaned_data.get("role") == FamilyMember.Role.ADMIN
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


@admin.register(FamilyMember)
class FamilyMemberAdmin(admin.ModelAdmin):
    list_display = ("user", "family", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__phone_number", "family__name")
    autocomplete_fields = ("user", "family")
    readonly_fields = ("created_at",)
