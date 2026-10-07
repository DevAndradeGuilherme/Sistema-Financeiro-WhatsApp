import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.accounts.models import User
from apps.categories.models import Category, CategoryKind, Subcategory
from apps.categories.seed import INITIAL_CATALOG, seed_categories
from apps.categories.selectors import list_active_categories
from apps.transactions.models import TransactionType

pytestmark = pytest.mark.django_db


def test_category_accepts_compatible_types():
    both = Category(name="Pix", slug="pix", kind=CategoryKind.BOTH)
    expense = Category(name="Lazer", slug="lazer", kind=CategoryKind.EXPENSE)
    assert both.accepts(TransactionType.EXPENSE)
    assert both.accepts(TransactionType.INCOME)
    assert expense.accepts(TransactionType.EXPENSE)
    assert not expense.accepts(TransactionType.INCOME)


def test_aliases_are_normalized():
    category = Category(
        name=" Lazer ", slug="lazer", kind="EXPENSE", aliases=[" Cinema", "cinema", "CINEMA  3D"]
    )
    category.full_clean()
    assert category.name == "Lazer"
    assert category.aliases == ["cinema", "cinema 3d"]


def test_category_name_unique_case_insensitive(transport):
    with pytest.raises(ValidationError):
        Category(name="TRANSPORTE", slug="transporte-2", kind="EXPENSE").full_clean()


def test_invalid_kind_rejected_by_database():
    with pytest.raises(IntegrityError):
        Category.objects.create(name="X", slug="x", kind="OTHER")


def test_subcategory_slug_unique_within_category(transport, fuel):
    with pytest.raises(IntegrityError):
        Subcategory.objects.create(category=transport, name="Outro", slug="combustivel")


def test_category_in_use_cannot_change_to_incompatible_kind(alice, transport, make_transaction):
    make_transaction(user=alice, category=transport)
    transport.kind = CategoryKind.INCOME
    with pytest.raises(ValidationError):
        transport.full_clean()
    transport.kind = CategoryKind.BOTH
    transport.full_clean()


def test_used_subcategory_cannot_move_to_other_category(
    alice, transport, fuel, food, make_transaction
):
    make_transaction(user=alice, category=transport, subcategory=fuel)
    fuel.category = food
    with pytest.raises(ValidationError):
        fuel.full_clean()


def test_list_active_categories_hides_inactive(transport, fuel, food):
    Subcategory.objects.create(category=transport, name="Pedágio", slug="pedagio", is_active=False)
    food.is_active = False
    food.save()
    categories = list(list_active_categories())
    assert categories == [transport]
    assert categories[0].active_subcategories == [fuel]


def test_seed_is_idempotent_and_preserves_admin_changes():
    first = seed_categories()
    assert first["categories"] == len(INITIAL_CATALOG)
    assert first["subcategories"] == 6
    transport = Category.objects.get(slug="transporte")
    transport.name = "Mobilidade"
    transport.save()

    second = seed_categories()
    assert second == {"categories": 0, "subcategories": 0}
    assert Category.objects.get(slug="transporte").name == "Mobilidade"
    assert Category.objects.get(slug="salario").kind == CategoryKind.INCOME


def test_only_superuser_can_manage_categories(client, transport):
    staff = User.objects.create_user(username="staff", password="pw-123456", is_staff=True)
    staff.user_permissions.set(Permission.objects.filter(content_type__app_label="categories"))
    client.force_login(staff)

    assert client.get("/admin/categories/category/").status_code == 200  # view allowed
    assert client.get("/admin/categories/category/add/").status_code == 403
    change_url = f"/admin/categories/category/{transport.pk}/change/"
    response = client.post(
        change_url, {"name": "Hack", "slug": "transporte", "kind": "EXPENSE", "sort_order": 0}
    )
    assert response.status_code == 403
    delete_url = f"/admin/categories/category/{transport.pk}/delete/"
    assert client.post(delete_url, {"post": "yes"}).status_code == 403
    transport.refresh_from_db()
    assert transport.name == "Transporte"


def test_superuser_can_add_category(admin_client):
    response = admin_client.post(
        "/admin/categories/category/add/",
        {
            "name": "Pets",
            "slug": "pets",
            "kind": "EXPENSE",
            "aliases": "Ração, veterinário",
            "is_active": "on",
            "sort_order": 0,
            "subcategories-TOTAL_FORMS": "0",
            "subcategories-INITIAL_FORMS": "0",
        },
    )
    assert response.status_code == 302, response.content
    assert Category.objects.get(slug="pets").aliases == ["ração", "veterinário"]
