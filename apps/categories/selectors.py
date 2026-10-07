from django.db.models import Prefetch, QuerySet

from .models import Category, Subcategory


def get_fallback_category(kind: str) -> Category | None:
    """The active fallback category for EXPENSE or INCOME (explicit flag, not by name)."""
    return Category.objects.filter(kind=kind, is_fallback=True, is_active=True).first()


def list_active_categories() -> QuerySet[Category]:
    """Active catalog with active subcategories (input for the AI context)."""
    return Category.objects.filter(is_active=True).prefetch_related(
        Prefetch(
            "subcategories",
            queryset=Subcategory.objects.filter(is_active=True),
            to_attr="active_subcategories",
        )
    )
