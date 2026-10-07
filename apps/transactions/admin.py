from django.contrib import admin

from .models import Transaction


class DeletedFilter(admin.SimpleListFilter):
    title = "situação"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return (("active", "Ativas"), ("deleted", "Excluídas"))

    def queryset(self, request, queryset):
        if self.value() == "active":
            return queryset.active()
        if self.value() == "deleted":
            return queryset.deleted()
        return queryset


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    """Read-only diagnostics view.

    Financial data is created only through ``services.create_transaction`` and changed only by
    its owner, so the admin cannot add, edit or physically delete transactions.
    Soft-deleted rows are shown (filterable) for diagnosis.
    """

    list_display = (
        "transaction_date",
        "user",
        "type",
        "amount",
        "category",
        "subcategory",
        "source",
        "family",
        "deleted_at",
        "created_at",
    )
    list_filter = (DeletedFilter, "type", "source", "category", "transaction_date")
    search_fields = ("id", "description", "user__username", "user__phone_number", "idempotency_key")
    list_select_related = ("user", "category", "subcategory", "family")
    date_hierarchy = "transaction_date"
    ordering = ("-transaction_date", "-created_at")
    readonly_fields = [field.name for field in Transaction._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
