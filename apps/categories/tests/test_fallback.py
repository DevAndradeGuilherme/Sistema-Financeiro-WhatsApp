import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.categories.models import Category, CategoryKind
from apps.categories.seed import seed_categories
from apps.categories.selectors import get_fallback_category

pytestmark = pytest.mark.django_db


def make(name, kind=CategoryKind.EXPENSE, **kwargs):
    return Category.objects.create(name=name, slug=name.lower(), kind=kind, **kwargs)


def test_get_fallback_category_by_flag_not_by_name():
    make("Outros gastos")  # name alone means nothing
    flagged = make("Diversos", is_fallback=True)
    assert get_fallback_category("EXPENSE") == flagged
    assert get_fallback_category("INCOME") is None


def test_single_fallback_per_kind_enforced_by_database():
    make("Diversos", is_fallback=True)
    make("Extras", kind=CategoryKind.INCOME, is_fallback=True)  # other kind is fine
    with pytest.raises(IntegrityError):
        make("Outros", is_fallback=True)


def test_fallback_must_be_active():
    with pytest.raises(IntegrityError):
        make("Diversos", is_fallback=True, is_active=False)


def test_fallback_cannot_be_both_kind():
    with pytest.raises(IntegrityError):
        make("Pix", kind=CategoryKind.BOTH, is_fallback=True)


def test_ambiguous_fallback_reported_by_validation():
    make("Diversos", is_fallback=True)
    duplicate = Category(name="Outros", slug="outros", kind="EXPENSE", is_fallback=True)
    with pytest.raises(ValidationError):
        duplicate.full_clean()


def test_seed_marks_fallbacks_idempotently():
    seed_categories()
    seed_categories()
    assert get_fallback_category("EXPENSE").slug == "outros-gastos"
    assert get_fallback_category("INCOME").slug == "outras-receitas"
    assert Category.objects.filter(is_fallback=True).count() == 2


def test_seed_does_not_override_admin_fallback():
    chosen = make("Diversos", is_fallback=True)
    seed_categories()
    assert get_fallback_category("EXPENSE") == chosen
    assert not Category.objects.get(slug="outros-gastos").is_fallback


def test_seed_marks_existing_unflagged_catalog():
    """Catalogs seeded before the flag existed get their fallback on the next seed."""
    seed_categories()
    Category.objects.update(is_fallback=False)
    seed_categories()
    assert get_fallback_category("EXPENSE").slug == "outros-gastos"
