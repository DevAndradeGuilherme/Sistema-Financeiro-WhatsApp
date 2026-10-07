from django.contrib import admin

from .models import Category, Subcategory


class SuperuserCatalogMixin:
    """Catalog is managed only by superusers; other staff may only view."""

    def has_add_permission(self, request, *args):
        return request.user.is_active and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser


class SubcategoryInline(SuperuserCatalogMixin, admin.TabularInline):
    model = Subcategory
    extra = 0
    fields = ("name", "slug", "aliases", "is_active", "sort_order")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Category)
class CategoryAdmin(SuperuserCatalogMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "kind", "is_active", "sort_order", "updated_at")
    list_filter = ("kind", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    inlines = [SubcategoryInline]


@admin.register(Subcategory)
class SubcategoryAdmin(SuperuserCatalogMixin, admin.ModelAdmin):
    list_display = ("name", "category", "slug", "is_active", "sort_order")
    list_filter = ("is_active", "category")
    search_fields = ("name", "slug", "category__name")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("category",)
