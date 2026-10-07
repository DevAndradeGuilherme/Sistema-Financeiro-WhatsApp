from django.db.models import Prefetch, QuerySet

from .models import Category, Subcategory


def list_active_categories() -> QuerySet[Category]:
    """Active catalog with active subcategories. Future input for the AI context."""
    return Category.objects.filter(is_active=True).prefetch_related(
        Prefetch(
            "subcategories",
            queryset=Subcategory.objects.filter(is_active=True),
            to_attr="active_subcategories",
        )
    )
